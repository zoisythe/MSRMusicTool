from __future__ import annotations

import os

import httpx
import pytest

from msr_music_tool.site import MonsterSirenClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MSR_RUN_LIVE_TESTS") != "1",
        reason="set MSR_RUN_LIVE_TESTS=1 to check the live service",
    ),
]


@pytest.mark.asyncio
async def test_live_catalog_detail_and_media_headers() -> None:
    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        client = MonsterSirenClient(http)
        albums, songs = await client.fetch_catalog()
        assert albums
        assert songs

        album = await client.fetch_album(albums[0].cid)
        assert album.songs
        song = await client.fetch_song(album.songs[0].cid)

        cover_status, cover_headers = await _head(http, album.cover_url)
        audio_status, audio_headers = await _head(http, song.source_url)
        assert cover_status < 400
        assert audio_status < 400
        assert cover_headers.get("Content-Type", "").startswith("image/")
        assert audio_headers.get("Content-Type", "").startswith(("audio/", "application/"))


async def _head(http: httpx.AsyncClient, url: str) -> tuple[int, httpx.Headers]:
    response = await http.head(url)
    if response.status_code in {403, 405, 501}:
        async with http.stream("GET", url, headers={"Range": "bytes=0-0"}) as streamed:
            return streamed.status_code, httpx.Headers(streamed.headers)
    return response.status_code, response.headers
