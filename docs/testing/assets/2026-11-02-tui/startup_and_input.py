from __future__ import annotations

# Imports are timed; polling observes app state without changing its event handling.
# ruff: noqa: E402, ASYNC110
import asyncio
import json
import sys
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

if TYPE_CHECKING:
    import httpx

import_started = perf_counter()
from textual import events

import msr_music_tool.app as module
from msr_music_tool.app import CatalogScreen, MonsterSirenApp
from msr_music_tool.catalog import Catalog
from msr_music_tool.config import AppConfig
from msr_music_tool.models import AlbumDetail, AlbumSummary, SongSummary
from msr_music_tool.site import MonsterSirenClient

HERE = Path(__file__).resolve().parent
result = {
    "python": sys.version.split()[0],
    "app_import_ms": round((perf_counter() - import_started) * 1000, 2),
}


async def real_startup():
    data = []
    phase = "startup"
    client_factory = module._http_client
    get_json = MonsterSirenClient._get_json

    def timed_client() -> httpx.AsyncClient:
        before = perf_counter()
        client = client_factory()
        data.append(
            {
                "phase": phase,
                "component": "http_client_construct",
                "ms": round((perf_counter() - before) * 1000, 2),
            }
        )
        return client

    async def timed_json(self, path: str) -> Any:
        before = perf_counter()
        try:
            return await get_json(self, path)
        finally:
            data.append(
                {
                    "phase": phase,
                    "component": path,
                    "ms": round((perf_counter() - before) * 1000, 2),
                }
            )

    client_patch = patch.object(module, "_http_client", timed_client)
    client_patch.start()
    MonsterSirenClient._get_json = timed_json
    app = MonsterSirenApp(AppConfig(HERE / "unused.toml", HERE / "unused", False))
    before = perf_counter()
    try:
        async with app.run_test(size=(100, 32)) as pilot:
            first_screen = round((perf_counter() - before) * 1000, 2)
            async with asyncio.timeout(25):
                while not isinstance(app.screen, CatalogScreen) or app._loading:
                    await asyncio.sleep(0.01)
                await pilot.pause(0)
            ready = round((perf_counter() - before) * 1000, 2)
            phase = "uncached_detail"
            before = perf_counter()
            await pilot.press("right")
            await pilot.pause(0)
            detail_time = round((perf_counter() - before) * 1000, 2)
        return {
            "first_loading_screen_ms": first_screen,
            "catalog_ready_ms": ready,
            "uncached_detail_ms": detail_time,
            "components": data,
        }
    finally:
        client_patch.stop()
        MonsterSirenClient._get_json = get_json


async def waiting_action():
    album = AlbumSummary("a", "Album", "https://cdn.test/a.jpg", (), 0)
    song = SongSummary("s", "Song", "a", ())
    app = MonsterSirenApp(
        AppConfig(HERE / "unused.toml", HERE / "unused", False), catalog=Catalog((album,), (song,))
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_detail(cid: str) -> AlbumDetail:
        entered.set()
        await release.wait()
        return AlbumDetail("a", "Album", "", album.cover_url, (song,))

    with patch.object(app, "get_album_detail", delayed_detail):
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0)
            driver = app._driver
            assert driver is not None
            driver.send_message(events.Key("right", None))
            await asyncio.wait_for(entered.wait(), 2)
            before = perf_counter()
            driver.send_message(events.Key("ctrl+c", None))
            await asyncio.sleep(0.15)
            snapshot = {
                "wait_before_release_ms": round((perf_counter() - before) * 1000, 2),
                "quit_processed_while_detail_pending": app._quitting,
                "still_running_while_detail_pending": app.is_running,
            }
            release.set()
            async with asyncio.timeout(3):
                while app.is_running:
                    await asyncio.sleep(0.01)
            snapshot["ctrl_c_until_exit_ms"] = round((perf_counter() - before) * 1000, 2)
    return snapshot


async def main():
    result["real_startup"] = await real_startup()
    result["input_while_waiting_detail"] = await waiting_action()
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    (HERE / "startup-and-input.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    asyncio.run(main())
