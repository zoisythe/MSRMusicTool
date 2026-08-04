from __future__ import annotations

import httpx
import pytest

from msr_music_tool.site import MonsterSirenClient, SiteError


@pytest.mark.asyncio
async def test_catalog_and_details_are_parsed_into_typed_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        responses = {
            "/api/albums": {
                "code": 0,
                "data": [
                    {
                        "cid": "a1",
                        "name": "Album",
                        "coverUrl": "https://cdn.test/a.jpg",
                        "artistes": ["MSR"],
                    }
                ],
            },
            "/api/songs": {
                "code": 0,
                "data": {
                    "list": [
                        {
                            "cid": "s1",
                            "name": "Song",
                            "albumCid": "a1",
                            "artists": ["MSR"],
                        }
                    ]
                },
            },
            "/api/album/a1/detail": {
                "code": 0,
                "data": {
                    "cid": "a1",
                    "name": "Album",
                    "intro": "Intro",
                    "coverUrl": "https://cdn.test/a.jpg",
                    "songs": [{"cid": "s1", "name": "Song", "artistes": ["MSR"]}],
                },
            },
            "/api/song/s1": {
                "code": 0,
                "data": {
                    "cid": "s1",
                    "name": "Song",
                    "albumCid": "a1",
                    "sourceUrl": "https://cdn.test/s1.wav",
                    "lyricUrl": None,
                    "artists": ["MSR"],
                },
            },
        }
        return httpx.Response(200, json=responses[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = MonsterSirenClient(http, max_retries=0)
        albums, songs = await client.fetch_catalog()
        album = await client.fetch_album("a1")
        song = await client.fetch_song("s1")

    assert albums[0].release_rank == 0
    assert songs[0].album_cid == "a1"
    assert album.intro == "Intro"
    assert album.songs == songs
    assert song.lyric_url is None


@pytest.mark.asyncio
async def test_transient_status_is_retried() -> None:
    attempts = 0
    delays: list[float] = []

    async def no_sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "cid": "s1",
                    "name": "Song",
                    "albumCid": "a1",
                    "sourceUrl": "https://cdn.test/s1.wav",
                    "lyricUrl": None,
                    "artists": [],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = MonsterSirenClient(http, max_retries=1, sleep=no_sleep)
        await client.fetch_song("s1")

    assert attempts == 2
    assert delays == [2.0]


@pytest.mark.asyncio
async def test_nonzero_api_code_is_an_error() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"code": 7, "msg": "bad", "data": None})
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(SiteError, match="bad"):
            await MonsterSirenClient(http, max_retries=0).fetch_song("s1")


@pytest.mark.asyncio
async def test_nontransient_http_status_is_not_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SiteError, match="HTTP 404"):
            await MonsterSirenClient(http, max_retries=3).fetch_song("missing")

    assert attempts == 1
