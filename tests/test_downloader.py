from __future__ import annotations

from collections import Counter
from pathlib import Path

import httpx
import pytest

from msr_music_tool.catalog import Catalog
from msr_music_tool.downloader import BatchDownloader, SongOutcome
from msr_music_tool.models import AlbumDetail, AlbumDownload, AlbumSummary, SongSummary
from msr_music_tool.naming import FileNamer
from msr_music_tool.site import MonsterSirenClient


async def no_sleep(_: float) -> None:
    return None


def _download_fixture() -> tuple[Catalog, AlbumDownload]:
    summary = AlbumSummary("a1", "Album", "https://cdn.test/cover.jpg", (), 0)
    songs = (
        SongSummary("s1", "With lyric", "a1", ()),
        SongSummary("s2", "No lyric", "a1", ()),
    )
    catalog = Catalog((summary,), songs)
    detail = AlbumDetail("a1", "Album", "fallback intro", summary.cover_url, songs)
    return catalog, AlbumDownload(detail, songs)


@pytest.mark.asyncio
async def test_downloads_cover_once_and_writes_remote_and_fallback_lyrics(tmp_path: Path) -> None:
    catalog, album_download = _download_fixture()
    calls: Counter[tuple[str, str]] = Counter()
    assets = {
        "/cover.jpg": (b"cover", "image/jpeg"),
        "/s1.wav": (b"audio-one", "audio/wav"),
        "/s2.flac": (b"audio-two", "audio/flac"),
        "/s1.lrc": (b"[00:01] lyric", "text/plain"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls[(request.method, request.url.path)] += 1
        if request.url.host == "monster-siren.hypergryph.com":
            cid = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "cid": cid,
                        "name": catalog.song_by_cid[cid].name,
                        "albumCid": "a1",
                        "sourceUrl": f"https://cdn.test/{cid}.{'wav' if cid == 's1' else 'flac'}",
                        "lyricUrl": "https://cdn.test/s1.lrc" if cid == "s1" else None,
                        "artists": [],
                    },
                },
            )
        content, media_type = assets[request.url.path]
        headers = {"Content-Length": str(len(content)), "Content-Type": media_type}
        return httpx.Response(
            200,
            headers=headers,
            content=b"" if request.method == "HEAD" else content,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        site = MonsterSirenClient(http, max_retries=0)
        downloader = BatchDownloader(
            site,
            http,
            tmp_path,
            FileNamer(catalog),
            max_retries=0,
            sleep=no_sleep,
        )
        first = await downloader.execute((album_download,))
        second = await downloader.execute((album_download,))

    assert [result.outcome for result in first.songs] == [SongOutcome.SUCCESS, SongOutcome.SUCCESS]
    assert [result.outcome for result in second.songs] == [SongOutcome.SKIPPED, SongOutcome.SKIPPED]
    assert calls[("GET", "/cover.jpg")] == 1
    assert (tmp_path / "塞壬唱片-MSR - With lyric.lrc").read_bytes() == b"[00:01] lyric"
    assert (tmp_path / "塞壬唱片-MSR - No lyric.lrc").read_text("utf-8") == "fallback intro"
    assert (tmp_path / "塞壬唱片-MSR - With lyric.jpg").read_bytes() == b"cover"
    assert (tmp_path / "塞壬唱片-MSR - No lyric.jpg").read_bytes() == b"cover"
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.asyncio
async def test_partial_failure_does_not_abort_other_songs(tmp_path: Path) -> None:
    catalog, album_download = _download_fixture()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "monster-siren.hypergryph.com":
            cid = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "cid": cid,
                        "name": catalog.song_by_cid[cid].name,
                        "albumCid": "a1",
                        "sourceUrl": f"https://cdn.test/{cid}.wav",
                        "lyricUrl": None,
                        "artists": [],
                    },
                },
            )
        if request.method == "HEAD":
            return httpx.Response(405)
        if request.url.path == "/s1.wav":
            return httpx.Response(500)
        return httpx.Response(
            200,
            headers={"Content-Length": "5", "Content-Type": "application/octet-stream"},
            content=b"cover" if request.url.path == "/cover.jpg" else b"audio",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        site = MonsterSirenClient(http, max_retries=0)
        result = await BatchDownloader(
            site,
            http,
            tmp_path,
            FileNamer(catalog),
            max_retries=1,
            sleep=no_sleep,
        ).execute((album_download,))

    assert [song.outcome for song in result.songs] == [SongOutcome.FAILED, SongOutcome.SUCCESS]
    assert result.failed_ids == {"s1"}
    assert result.successful_ids == {"s2"}
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.asyncio
async def test_content_type_selects_extension_when_audio_url_has_no_suffix(
    tmp_path: Path,
) -> None:
    summary = AlbumSummary("a1", "Album", "https://cdn.test/cover.png", (), 0)
    song = SongSummary("s1", "No suffix", "a1", ())
    catalog = Catalog((summary,), (song,))
    prepared = AlbumDownload(
        AlbumDetail("a1", "Album", "", summary.cover_url, (song,)),
        (song,),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "monster-siren.hypergryph.com":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "cid": "s1",
                        "name": "No suffix",
                        "albumCid": "a1",
                        "sourceUrl": "https://cdn.test/audio",
                        "lyricUrl": None,
                        "artists": [],
                    },
                },
            )
        if request.method == "HEAD" and request.url.path == "/audio":
            return httpx.Response(405)
        content = b"cover" if request.url.path == "/cover.png" else b"audio"
        content_type = "image/png" if request.url.path == "/cover.png" else "audio/flac"
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(content)), "Content-Type": content_type},
            content=b"" if request.method == "HEAD" else content,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await BatchDownloader(
            MonsterSirenClient(http, max_retries=0),
            http,
            tmp_path,
            FileNamer(catalog),
            max_retries=0,
            sleep=no_sleep,
        ).execute((prepared,))

    assert result.songs[0].outcome is SongOutcome.SUCCESS
    assert (tmp_path / "塞壬唱片-MSR - No suffix.flac").read_bytes() == b"audio"
