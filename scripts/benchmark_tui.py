"""Measure catalog interaction and disk-cache startup without downloading music."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import statistics
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import httpx

import msr_music_tool.app as app_module
from msr_music_tool.app import CatalogOptions, CatalogScreen, MonsterSirenApp
from msr_music_tool.catalog import Catalog
from msr_music_tool.catalog_cache import CachedCatalog, save_catalog_cache
from msr_music_tool.config import AppConfig
from msr_music_tool.models import AlbumDetail, AlbumSummary, SongSummary


async def wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(3000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise TimeoutError("TUI did not finish the requested operation")


async def measure_catalog(catalog: Catalog, directory: Path) -> dict:
    details = [
        AlbumDetail(a.cid, a.name, "", a.cover_url, catalog.songs_by_album.get(a.cid, ()))
        for a in catalog.albums
    ]
    app = MonsterSirenApp(
        AppConfig(directory / "config.toml", directory / "out", False),
        catalog=catalog,
        album_details=details,
    )
    timings = {}
    heartbeat = []
    active = False
    phase = "startup"
    refreshes = []
    original_refresh = CatalogScreen.refresh_albums

    def refresh(self, *, target_cid: str | None = None) -> None:
        before = perf_counter()
        original_refresh(self, target_cid=target_cid)
        refreshes.append({"phase": phase, "ms": (perf_counter() - before) * 1000})

    async def monitor():
        while active:
            before = perf_counter()
            await asyncio.sleep(0.01)
            if active:
                heartbeat.append(max(0, (perf_counter() - before - 0.01) * 1000))

    with patch.object(CatalogScreen, "refresh_albums", refresh):
        before = perf_counter()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0)
            timings["startup_ms"] = (perf_counter() - before) * 1000
            active = True
            probe = asyncio.create_task(monitor())
            try:

                async def press(label: str, *keys: str, settled: Callable[[], bool] | None = None):
                    nonlocal phase
                    phase = label
                    start = perf_counter()
                    await pilot.press(*keys)
                    if settled is not None:
                        await wait_until(settled)
                    await pilot.pause(0)
                    timings[label] = (perf_counter() - start) * 1000

                await press("down_ms", "down")
                await press("select_album_ms", "space")
                await press("deselect_album_ms", "space")
                await press("cached_detail_ms", "right")
                await press("back_to_catalog_ms", "left")
                await press("open_search_ms", "/")
                screen = app.screen
                assert isinstance(screen, CatalogScreen)
                await press("search_a_ms", "a", settled=lambda: screen.query_text == "a")
                await press("clear_search_ms", "backspace", settled=lambda: screen.query_text == "")
                await press("close_search_ms", "escape")
                await asyncio.sleep(0.02)
            finally:
                active = False
                await probe
            assert app.selection is not None and not app.selection.selected
            assert screen.query_one("#albums", CatalogOptions).option_count == len(catalog.albums)
    return {
        "timings": {key: round(value, 2) for key, value in timings.items()},
        "refreshes": refreshes,
        "interaction_max_lag_ms": round(max(heartbeat, default=0), 2),
    }


async def measure_cache_startup(catalog: Catalog, directory: Path) -> float:
    cache = directory / "catalog.json"
    save_catalog_cache(cache, CachedCatalog(catalog, datetime(2000, 1, 1, tzinfo=UTC)))

    async def pending_network(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("Network fixture should be cancelled on exit")

    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(pending_network))

    app = MonsterSirenApp(
        AppConfig(directory / "config.toml", directory / "out", False),
        cache_path=cache,
    )
    with patch.object(app_module, "_http_client", factory):
        before = perf_counter()
        async with app.run_test(size=(100, 32)) as pilot:
            await wait_until(lambda: isinstance(app.screen, CatalogScreen))
            await pilot.pause(0)
            elapsed = (perf_counter() - before) * 1000
            assert app.catalog is not None and app.catalog.songs == catalog.songs
            await pilot.press("ctrl+c")
    return round(elapsed, 2)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument(
        "--fixture", type=Path, default=Path("docs/testing/assets/2026-11-02-tui/catalog.json")
    )
    parser.add_argument("--output", type=Path, default=Path("build/tui-performance/after.json"))
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    raw = json.loads(args.fixture.read_text(encoding="utf-8"))
    # JSON lists must be restored to model tuples, as they are when reading the disk cache.
    catalog = Catalog(
        [AlbumSummary(**(a | {"artists": tuple(a["artists"])})) for a in raw["albums"]],
        [SongSummary(**(s | {"artists": tuple(s["artists"])})) for s in raw["songs"]],
    )
    results = []
    with tempfile.TemporaryDirectory(prefix="msr-tui-benchmark-") as directory:
        for _ in range(args.repeat):
            row = await measure_catalog(catalog, Path(directory))
            row["cached_startup_ms"] = await measure_cache_startup(catalog, Path(directory))
            results.append(row)
            print(json.dumps(row), flush=True)
    medians = {
        key: round(statistics.median(row["timings"][key] for row in results), 2)
        for key in results[0]["timings"]
    }
    cached = round(statistics.median(row["cached_startup_ms"] for row in results), 2)
    maximum_lag = max(row["interaction_max_lag_ms"] for row in results)
    gates = {
        "cached_startup_under_300_ms": cached <= 300,
        "selection_under_200_ms": medians["select_album_ms"] <= 200,
        "return_under_200_ms": medians["back_to_catalog_ms"] <= 200,
        "search_restore_under_200_ms": medians["clear_search_ms"] <= 200,
        "interaction_lag_under_100_ms": maximum_lag < 100,
    }
    report = {
        "python": sys.version,
        "executable": sys.executable,
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("MSRMusicTool", "textual", "rich", "httpx")
        },
        "app_path": app_module.__file__,
        "app_sha256": hashlib.sha256(Path(app_module.__file__).read_bytes()).hexdigest(),
        "fixture_sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
        "albums": len(catalog.albums),
        "songs": len(catalog.songs),
        "size": [100, 32],
        "method": "Pilot key dispatch plus rendering idle; search includes 60ms debounce; "
        "heartbeat excludes startup, assertions and teardown; disk-cache startup "
        "uses blocked mock network and excludes module imports",
        "runs": results,
        "median_timings_ms": medians,
        "median_cached_startup_ms": cached,
        "interaction_max_lag_ms": maximum_lag,
        "gates": gates,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "median_timings_ms": medians,
                "cached_startup_ms": cached,
                "interaction_max_lag_ms": maximum_lag,
                "gates": gates,
            }
        ),
        flush=True,
    )
    if not all(gates.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
