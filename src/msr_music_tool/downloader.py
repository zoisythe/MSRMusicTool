from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import httpx

from .models import AlbumDownload, SongSummary
from .naming import FileNamer, extension_from_remote
from .site import (
    TRANSIENT_STATUS_CODES,
    MonsterSirenClient,
    retry_delay_for_response,
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


@dataclass(frozen=True, slots=True)
class _RemoteMetadata:
    length: int | None
    content_type: str | None


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
    ) -> None:
        self._site = site
        self._http = http
        self._output_dir = output_dir
        self._namer = namer
        self._semaphore = asyncio.Semaphore(concurrency)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep

    async def execute(
        self,
        albums: Iterable[AlbumDownload],
        *,
        progress: ProgressCallback | None = None,
    ) -> BatchResult:
        prepared = tuple(albums)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        cover_tasks = {
            item.album.cid: asyncio.create_task(self._download_album_cover(item))
            for item in prepared
        }
        tasks = [
            asyncio.create_task(self._download_song(item, song, cover_tasks[item.album.cid]))
            for item in prepared
            for song in item.songs
        ]
        results: list[SongDownloadResult] = []
        total = len(tasks)
        for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
            result = await task
            results.append(result)
            if progress is not None:
                update = progress(
                    DownloadProgress(
                        completed=completed,
                        total=total,
                        song=result.song,
                        outcome=result.outcome,
                    )
                )
                if inspect.isawaitable(update):
                    await update

        order = {
            song.cid: index
            for index, song in enumerate(song for item in prepared for song in item.songs)
        }
        results.sort(key=lambda result: order[result.song.cid])
        return BatchResult(tuple(results))

    async def _download_song(
        self,
        item: AlbumDownload,
        song: SongSummary,
        cover_task: asyncio.Task[dict[str, AssetResult]],
    ) -> SongDownloadResult:
        try:
            cover_results = await cover_task
            cover = cover_results[song.cid]
        except Exception as exc:
            cover = AssetResult(
                "封面",
                AssetOutcome.FAILED,
                self._output_dir / self._namer.filename(song, ".img"),
                f"封面处理失败: {exc}",
            )
        try:
            async with self._semaphore:
                detail = await self._site.fetch_song(song.cid)
            audio_probe = await self._probe(detail.source_url)
            audio_ext = await self._resolve_extension(
                detail.source_url, audio_probe, default=".bin"
            )
            audio_path = self._output_dir / self._namer.filename(song, audio_ext)
            lyric_path = self._output_dir / self._namer.filename(song, ".lrc")
            audio_task = self._ensure_remote(
                "音频", detail.source_url, audio_path, probe=audio_probe
            )
            if detail.lyric_url:
                lyric_task = self._ensure_remote("歌词", detail.lyric_url, lyric_path)
            else:
                lyric_task = self._ensure_bytes(
                    "歌词", item.album.intro.encode("utf-8"), lyric_path
                )
            audio, lyric = await asyncio.gather(audio_task, lyric_task)
        except Exception as exc:  # Each song must produce a result without aborting the batch.
            error = f"读取歌曲信息失败: {exc}"
            audio = AssetResult(
                "音频",
                AssetOutcome.FAILED,
                self._output_dir / self._namer.filename(song, ".bin"),
                error,
            )
            lyric = AssetResult(
                "歌词",
                AssetOutcome.FAILED,
                self._output_dir / self._namer.filename(song, ".lrc"),
                error,
            )

        assets = (audio, lyric, cover)
        if any(asset.outcome is AssetOutcome.FAILED for asset in assets):
            outcome = SongOutcome.FAILED
        elif all(asset.outcome is AssetOutcome.SKIPPED for asset in assets):
            outcome = SongOutcome.SKIPPED
        else:
            outcome = SongOutcome.SUCCESS
        return SongDownloadResult(song=song, outcome=outcome, assets=assets)

    async def _download_album_cover(self, item: AlbumDownload) -> dict[str, AssetResult]:
        probe = await self._probe(item.album.cover_url)
        extension = await self._resolve_extension(item.album.cover_url, probe, default=".img")
        paths = {
            song.cid: self._output_dir / self._namer.filename(song, extension)
            for song in item.songs
        }
        results: dict[str, AssetResult] = {}
        pending: dict[str, Path] = {}
        for cid, path in paths.items():
            if _matches_probe(path, probe):
                results[cid] = AssetResult("封面", AssetOutcome.SKIPPED, path)
            else:
                pending[cid] = path
        if not pending:
            return results

        shared_temp = self._temporary_path("cover")
        try:
            await self._stream_remote(item.album.cover_url, shared_temp)
            for cid, path in pending.items():
                copy_temp = self._temporary_path("copy")
                try:
                    await asyncio.to_thread(shutil.copyfile, shared_temp, copy_temp)
                    os.replace(copy_temp, path)
                    results[cid] = AssetResult("封面", AssetOutcome.DOWNLOADED, path)
                except (OSError, shutil.Error) as exc:
                    copy_temp.unlink(missing_ok=True)
                    results[cid] = AssetResult("封面", AssetOutcome.FAILED, path, str(exc))
        except Exception as exc:
            for cid, path in pending.items():
                results[cid] = AssetResult("封面", AssetOutcome.FAILED, path, str(exc))
        finally:
            shared_temp.unlink(missing_ok=True)
        return results

    async def _ensure_remote(
        self,
        kind: str,
        url: str,
        path: Path,
        *,
        probe: _RemoteMetadata | None | object = _UNPROBED,
    ) -> AssetResult:
        metadata = await self._probe(url) if probe is _UNPROBED else probe
        assert metadata is None or isinstance(metadata, _RemoteMetadata)
        if _matches_probe(path, metadata):
            return AssetResult(kind, AssetOutcome.SKIPPED, path)
        temp = self._temporary_path(kind)
        try:
            await self._stream_remote(url, temp)
            os.replace(temp, path)
            return AssetResult(kind, AssetOutcome.DOWNLOADED, path)
        except Exception as exc:
            temp.unlink(missing_ok=True)
            return AssetResult(kind, AssetOutcome.FAILED, path, str(exc))

    async def _ensure_bytes(self, kind: str, content: bytes, path: Path) -> AssetResult:
        try:
            if path.exists() and path.read_bytes() == content:
                return AssetResult(kind, AssetOutcome.SKIPPED, path)
            temp = self._temporary_path(kind)
            try:
                temp.write_bytes(content)
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
            return AssetResult(kind, AssetOutcome.DOWNLOADED, path)
        except OSError as exc:
            return AssetResult(kind, AssetOutcome.FAILED, path, str(exc))

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

    async def _stream_remote(self, url: str, temp: Path) -> _RemoteMetadata:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            temp.unlink(missing_ok=True)
            try:
                async with (
                    self._semaphore,
                    self._http.stream(
                        "GET", url, headers={"Accept-Encoding": "identity"}
                    ) as response,
                ):
                    if response.status_code in TRANSIENT_STATUS_CODES:
                        raise _TransientMedia(response)
                    response.raise_for_status()
                    metadata = _metadata(response.headers)
                    with temp.open("wb") as file:
                        async for chunk in response.aiter_bytes():
                            file.write(chunk)
                if metadata.length is not None and temp.stat().st_size != metadata.length:
                    raise OSError(
                        f"下载长度不符：预期 {metadata.length}，实际 {temp.stat().st_size}"
                    )
                return metadata
            except httpx.HTTPStatusError as exc:
                last_error = exc
                break
            except (_TransientMedia, httpx.RequestError, OSError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                await self._sleep(_media_retry_delay(exc, attempt, self._backoff_base))
        temp.unlink(missing_ok=True)
        raise OSError(f"下载失败: {last_error}") from last_error

    def _temporary_path(self, label: str) -> Path:
        safe_label = "".join(char for char in label if char.isascii() and char.isalnum()) or "asset"
        return self._output_dir / f".msr-{safe_label}-{uuid.uuid4().hex}.part"


class _TransientMedia(Exception):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"HTTP {response.status_code}")


def _metadata(headers: httpx.Headers) -> _RemoteMetadata:
    length: int | None = None
    raw_length = headers.get("Content-Length")
    if raw_length:
        with suppress(ValueError):
            length = int(raw_length)
    return _RemoteMetadata(length=length, content_type=headers.get("Content-Type"))


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
