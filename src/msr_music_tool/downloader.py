from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import httpx

from .config import AppConfig
from .models import AlbumDownload, DownloadMode, SongSummary
from .naming import FileNamer, extension_from_remote
from .site import (
    TRANSIENT_STATUS_CODES,
    MonsterSirenClient,
    retry_delay_for_response,
)
from .transfers import (
    Aria2Backend,
    DiskIO,
    HttpBackend,
    RemoteMetadata,
    ResourceCallback,
    ResourceProgress,
    TransferBackend,
    metadata,
)

_UNPROBED = object()


class AssetOutcome(Enum):
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    FAILED = "failed"


class SongOutcome(Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AssetResult:
    kind: str
    outcome: AssetOutcome
    path: Path
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SongDownloadResult:
    song: SongSummary
    outcome: SongOutcome
    assets: tuple[AssetResult, ...]

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(asset.error for asset in self.assets if asset.error)


@dataclass(frozen=True, slots=True)
class BatchResult:
    songs: tuple[SongDownloadResult, ...]

    @property
    def successful_ids(self) -> frozenset[str]:
        return frozenset(
            result.song.cid for result in self.songs if result.outcome is not SongOutcome.FAILED
        )

    @property
    def failed_ids(self) -> frozenset[str]:
        return frozenset(
            result.song.cid for result in self.songs if result.outcome is SongOutcome.FAILED
        )

    def count(self, outcome: SongOutcome) -> int:
        return sum(result.outcome is outcome for result in self.songs)


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    completed: int
    total: int
    song: SongSummary
    outcome: SongOutcome


ProgressCallback = Callable[[DownloadProgress], Awaitable[None] | None]


_RemoteMetadata = RemoteMetadata


class BatchDownloader:
    def __init__(
        self,
        site: MonsterSirenClient,
        http: httpx.AsyncClient,
        output_dir: Path,
        namer: FileNamer,
        *,
        concurrency: int = 4,
        max_retries: int = 3,
        backoff_base: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        mode: DownloadMode = DownloadMode.ALL,
        config: AppConfig | None = None,
        backend: TransferBackend | None = None,
    ) -> None:
        self._site = site
        self._http = http
        self._output_dir = output_dir
        self._namer = namer
        concurrency = config.max_concurrent_downloads if config else concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        self._mode = mode
        self._disk = DiskIO(concurrency)
        self._workers = concurrency
        self._used = False
        self._make_backend = lambda: (
            backend
            or (
                Aria2Backend(config.rpc, self._disk, concurrency, output_dir)
                if config and config.backend == "aria2"
                else HttpBackend(http, self._disk, concurrency, max_retries, backoff_base, sleep)
            )
        )
        self._backend = self._make_backend()
        self._temps: set[Path] = set()
        self._resource_progress: ResourceCallback = lambda update: None
        self.unresolved_gids: tuple[str, ...] = ()
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep

    async def execute(
        self,
        albums: Iterable[AlbumDownload],
        *,
        progress: ProgressCallback | None = None,
        resource_progress: ResourceCallback | None = None,
    ) -> BatchResult:
        prepared = tuple(albums)
        if self._used:
            self._disk = DiskIO(self._workers)
            self._backend = self._make_backend()
        self._used = True
        self._temps.clear()
        self._resource_progress = resource_progress or (lambda update: None)
        tasks: list[asyncio.Task] = []
        try:
            await self._disk.run(self._output_dir.mkdir, 0o777, True, True)
            await self._backend.prepare()
            for item in prepared:
                if self._mode is DownloadMode.ALL:
                    self._emit(
                        f"cover:{item.album.cid}",
                        tuple(s.cid for s in item.songs),
                        f"{item.album.name} · 封面",
                        "waiting",
                    )
                for song in item.songs:
                    self._emit(
                        f"song:{song.cid}:音频", (song.cid,), f"{song.name} · 音频", "waiting"
                    )
                    if self._mode is not DownloadMode.AUDIO:
                        self._emit(
                            f"song:{song.cid}:歌词", (song.cid,), f"{song.name} · 歌词", "waiting"
                        )
            covers = {
                item.album.cid: asyncio.create_task(self._download_album_cover(item))
                for item in prepared
                if self._mode is DownloadMode.ALL
            }
            tasks.extend(covers.values())
            songs = [
                asyncio.create_task(self._download_song(item, song, covers.get(item.album.cid)))
                for item in prepared
                for song in item.songs
            ]
            tasks.extend(songs)
            results: list[SongDownloadResult] = []
            for completed, task in enumerate(asyncio.as_completed(songs), start=1):
                result = await task
                results.append(result)
                if progress is not None:
                    update = progress(
                        DownloadProgress(completed, len(songs), result.song, result.outcome)
                    )
                    if inspect.isawaitable(update):
                        await update
            order = {
                song.cid: index
                for index, song in enumerate(song for item in prepared for song in item.songs)
            }
            results.sort(key=lambda result: order[result.song.cid])
            return BatchResult(tuple(results))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                self.unresolved_gids = await self._backend.close()
                # Delete only files whose engine has confirmed it is no longer writing.
                for temp in self._temps:
                    if self._backend.can_delete(temp):
                        await self._disk.run(temp.unlink, True)
                        await self._disk.run(Path(str(temp) + ".aria2").unlink, True)
            finally:
                self._disk.close()

    def _emit(
        self,
        key: str,
        songs: tuple[str, ...],
        label: str,
        status: str,
        total: int | None = None,
        error: str | None = None,
    ) -> None:
        self._resource_progress(
            ResourceProgress(key, songs, label, status, total or 0, total, error=error)
        )

    async def _download_song(
        self,
        item: AlbumDownload,
        song: SongSummary,
        cover_task: asyncio.Task[dict[str, AssetResult]] | None,
    ) -> SongDownloadResult:
        assets: list[AssetResult] = []
        try:
            async with self._semaphore:
                detail = await self._site.fetch_song(song.cid)
            probe = await self._probe(detail.source_url)
            extension = await self._resolve_extension(detail.source_url, probe, default=".bin")
            path = self._output_dir / self._namer.filename(song, extension)
            jobs = [self._ensure_remote("音频", detail.source_url, path, (song.cid,), probe=probe)]
            if self._mode is not DownloadMode.AUDIO:
                lyric_path = self._output_dir / self._namer.filename(song, ".lrc")
                jobs.append(
                    self._ensure_remote("歌词", detail.lyric_url, lyric_path, (song.cid,))
                    if detail.lyric_url
                    else self._ensure_bytes(
                        "歌词", item.album.intro.encode("utf-8"), lyric_path, (song.cid,)
                    )
                )
            assets.extend(await asyncio.gather(*jobs))
        except Exception as exc:
            for kind in ("音频",) if self._mode is DownloadMode.AUDIO else ("音频", "歌词"):
                self._emit(
                    f"song:{song.cid}:{kind}",
                    (song.cid,),
                    f"{song.name} · {kind}",
                    "failed",
                    error=str(exc),
                )
            assets.append(
                AssetResult(
                    "歌曲信息", AssetOutcome.FAILED, self._output_dir, f"读取歌曲信息失败: {exc}"
                )
            )
        # Cover transfers run independently of audio/lyrics, then join at song completion.
        if cover_task is not None:
            try:
                assets.append((await cover_task)[song.cid])
            except Exception as exc:
                assets.append(
                    AssetResult(
                        "封面", AssetOutcome.FAILED, self._output_dir, f"封面处理失败: {exc}"
                    )
                )
        outcome = (
            SongOutcome.FAILED
            if any(a.outcome is AssetOutcome.FAILED for a in assets)
            else SongOutcome.SKIPPED
            if all(a.outcome is AssetOutcome.SKIPPED for a in assets)
            else SongOutcome.SUCCESS
        )
        return SongDownloadResult(song, outcome, tuple(assets))

    async def _download_album_cover(self, item: AlbumDownload) -> dict[str, AssetResult]:
        key = f"cover:{item.album.cid}"
        song_ids = tuple(song.cid for song in item.songs)
        label = f"{item.album.name} · 封面"
        probe = await self._probe(item.album.cover_url)
        extension = await self._resolve_extension(item.album.cover_url, probe, default=".img")
        paths = {
            song.cid: self._output_dir / self._namer.filename(song, extension)
            for song in item.songs
        }
        results: dict[str, AssetResult] = {}
        pending: dict[str, Path] = {}
        for cid, path in paths.items():
            if await self._disk.run(_matches_probe, path, probe):
                results[cid] = AssetResult("封面", AssetOutcome.SKIPPED, path)
            else:
                pending[cid] = path
        if not pending:
            self._emit(key, song_ids, label, "skipped", probe.length if probe else None)
            return results
        temp = self._temporary_path("cover")
        try:
            info = await self._backend.transfer(
                item.album.cover_url, temp, key, song_ids, label, self._resource_progress
            )
            if probe and probe.length is not None:
                await self._disk.run(_validate_size, temp, probe.length)
            for cid, path in pending.items():
                copy_temp = self._temporary_path("copy")
                try:
                    await self._disk.run(shutil.copyfile, temp, copy_temp)
                    await self._disk.run(os.replace, copy_temp, path)
                    results[cid] = AssetResult("封面", AssetOutcome.DOWNLOADED, path)
                except OSError as exc:
                    results[cid] = AssetResult("封面", AssetOutcome.FAILED, path, str(exc))
                finally:
                    await self._disk.run(copy_temp.unlink, True)
            failed = any(result.outcome is AssetOutcome.FAILED for result in results.values())
            self._emit(key, song_ids, label, "failed" if failed else "complete", info.length)
        except Exception as exc:
            self._emit(key, song_ids, label, "failed", error=str(exc))
            for cid, path in pending.items():
                results[cid] = AssetResult("封面", AssetOutcome.FAILED, path, str(exc))
        finally:
            if self._backend.can_delete(temp):
                await self._disk.run(temp.unlink, True)
        return results

    async def _ensure_remote(
        self,
        kind: str,
        url: str,
        path: Path,
        songs: tuple[str, ...],
        *,
        probe: RemoteMetadata | None | object = _UNPROBED,
    ) -> AssetResult:
        key, label = f"song:{songs[0]}:{kind}", f"{path.stem} · {kind}"
        info = await self._probe(url) if probe is _UNPROBED else probe
        assert info is None or isinstance(info, RemoteMetadata)
        if await self._disk.run(_matches_probe, path, info):
            self._emit(key, songs, label, "skipped", info.length if info else None)
            return AssetResult(kind, AssetOutcome.SKIPPED, path)
        temp = self._temporary_path(kind)
        try:
            transferred = await self._backend.transfer(
                url, temp, key, songs, label, self._resource_progress
            )
            if info and info.length is not None:
                await self._disk.run(_validate_size, temp, info.length)
            await self._disk.run(os.replace, temp, path)
            self._emit(key, songs, label, "complete", transferred.length)
            return AssetResult(kind, AssetOutcome.DOWNLOADED, path)
        except Exception as exc:
            self._emit(key, songs, label, "failed", error=str(exc))
            return AssetResult(kind, AssetOutcome.FAILED, path, str(exc))
        finally:
            if self._backend.can_delete(temp):
                await self._disk.run(temp.unlink, True)

    async def _ensure_bytes(
        self,
        kind: str,
        content: bytes,
        path: Path,
        songs: tuple[str, ...],
    ) -> AssetResult:
        key, label = f"song:{songs[0]}:{kind}", f"{path.stem} · {kind}"
        temp = self._temporary_path(kind)
        try:
            if await self._disk.run(lambda: path.exists() and path.read_bytes() == content):
                self._emit(key, songs, label, "skipped", len(content))
                return AssetResult(kind, AssetOutcome.SKIPPED, path)
            await self._disk.run(temp.write_bytes, content)
            await self._disk.run(os.replace, temp, path)
            self._emit(key, songs, label, "complete", len(content))
            return AssetResult(kind, AssetOutcome.DOWNLOADED, path)
        except OSError as exc:
            self._emit(key, songs, label, "failed", error=str(exc))
            return AssetResult(kind, AssetOutcome.FAILED, path, str(exc))
        finally:
            await self._disk.run(temp.unlink, True)

    async def _probe(self, url: str) -> _RemoteMetadata | None:
        for attempt in range(self._max_retries + 1):
            try:
                async with self._semaphore:
                    response = await self._http.head(url, headers={"Accept-Encoding": "identity"})
                if response.status_code in {403, 405, 501}:
                    return None
                if response.status_code in TRANSIENT_STATUS_CODES:
                    raise _TransientMedia(response)
                response.raise_for_status()
                return _metadata(response.headers)
            except httpx.HTTPStatusError:
                return None
            except (_TransientMedia, httpx.RequestError):
                if attempt >= self._max_retries:
                    return None
                await self._sleep(self._backoff_base * (2**attempt))
        return None

    async def _resolve_extension(
        self,
        url: str,
        probe: _RemoteMetadata | None,
        *,
        default: str,
    ) -> str:
        extension = extension_from_remote(url, default="")
        if extension:
            return extension
        metadata = probe
        if metadata is None or not metadata.content_type:
            metadata = await self._probe_with_streamed_get(url)
        return extension_from_remote(
            url,
            metadata.content_type if metadata else None,
            default=default,
        )

    async def _probe_with_streamed_get(self, url: str) -> _RemoteMetadata | None:
        for attempt in range(self._max_retries + 1):
            try:
                async with (
                    self._semaphore,
                    self._http.stream(
                        "GET",
                        url,
                        headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"},
                    ) as response,
                ):
                    if response.status_code in TRANSIENT_STATUS_CODES:
                        raise _TransientMedia(response)
                    response.raise_for_status()
                    return _metadata(response.headers)
            except httpx.HTTPStatusError:
                return None
            except (_TransientMedia, httpx.RequestError) as exc:
                if attempt >= self._max_retries:
                    return None
                await self._sleep(_media_retry_delay(exc, attempt, self._backoff_base))
        return None

    def _temporary_path(self, label: str) -> Path:
        safe_label = "".join(char for char in label if char.isascii() and char.isalnum()) or "asset"
        path = self._output_dir / f".msr-{safe_label}-{uuid.uuid4().hex}.part"
        self._temps.add(path)
        return path


class _TransientMedia(Exception):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"HTTP {response.status_code}")


_metadata = metadata


def _matches_probe(path: Path, probe: _RemoteMetadata | None) -> bool:
    return bool(
        probe is not None
        and probe.length is not None
        and path.exists()
        and path.is_file()
        and path.stat().st_size == probe.length
    )


def _media_retry_delay(error: Exception, attempt: int, base: float) -> float:
    if isinstance(error, _TransientMedia):
        return retry_delay_for_response(error.response, attempt, base)
    return base * (2**attempt)


def _validate_size(path: Path, expected: int) -> None:
    if path.stat().st_size != expected:
        raise OSError(f"下载长度不符：预期 {expected}，实际 {path.stat().st_size}")
