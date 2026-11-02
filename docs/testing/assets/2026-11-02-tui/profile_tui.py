from __future__ import annotations

import argparse
import asyncio
import cProfile
import dataclasses
import importlib.metadata
import json
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import textual.color as color_module
from textual.widgets import Input, Label, ListView, Static

import msr_music_tool.app as app_module
from msr_music_tool.app import CatalogScreen, MonsterSirenApp
from msr_music_tool.catalog import Catalog
from msr_music_tool.config import AppConfig
from msr_music_tool.models import AlbumDetail, AlbumSummary, SongSummary
from msr_music_tool.service import http_client
from msr_music_tool.site import MonsterSirenClient

HERE = Path(__file__).resolve().parent


async def load_catalog(live: bool, count: int) -> Catalog:
    fixture = HERE / "catalog.json"
    if live or not fixture.exists() and count == 0:
        started = perf_counter()
        async with http_client() as http:
            albums, songs = await asyncio.wait_for(MonsterSirenClient(http).fetch_catalog(), 25)
        fixture.write_text(
            json.dumps(
                {
                    "albums": [dataclasses.asdict(a) for a in albums],
                    "songs": [dataclasses.asdict(s) for s in songs],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "live_catalog_ms": round((perf_counter() - started) * 1000, 2),
                    "albums": len(albums),
                    "songs": len(songs),
                }
            ),
            flush=True,
        )
    if count:
        return Catalog(
            [
                AlbumSummary(str(i), f"Album {i:04}", "https://cdn.test/cover.jpg", (), i)
                for i in range(count)
            ],
            [SongSummary(f"s{i}", f"Track {i:04}", str(i), ()) for i in range(count)],
        )
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    return Catalog(
        [AlbumSummary(**a) for a in raw["albums"]], [SongSummary(**s) for s in raw["songs"]]
    )


async def run(catalog: Catalog, profile: bool, variant: str) -> dict:
    refreshes = []
    original = CatalogScreen.refresh_albums
    original_album_label = app_module._album_label
    original_song_label = app_module._song_label
    original_suggestion = color_module.get_suggestion
    suggestions = Counter()
    phase = "startup"

    def counted_suggestion(word: str, possible_words: Sequence[str]) -> str | None:
        suggestions[word] += 1
        return original_suggestion(word, possible_words)

    patches = ExitStack()
    patches.enter_context(patch.object(color_module, "get_suggestion", counted_suggestion))
    if "hex" in variant:
        patches.enter_context(
            patch.object(
                app_module,
                "_album_label",
                lambda match: original_album_label(match).replace("grey50", "#808080"),
            )
        )
        patches.enter_context(
            patch.object(
                app_module,
                "_song_label",
                lambda song, selected: original_song_label(song, selected).replace(
                    "grey50", "#808080"
                ),
            )
        )

    async def reuse_refresh(screen, target_cid=None):
        selection = screen.app.selection
        view = screen.query_one("#albums", ListView)
        matches = selection.album_matches(screen.query_text)
        existing = {item.album_cid: item for item in view.children}
        if set(existing) != {match.album.cid for match in matches}:
            await original(screen, target_cid=target_cid)
            for item, match in zip(view.children, matches, strict=True):
                item._diagnosis_markup = app_module._album_label(match)
            return
        search = screen.query_one("#search", Input)
        search_has_focus = search.display and screen.focused is search
        target_cid = target_cid or screen.current_album_cid
        for match in matches:
            row = existing[match.album.cid]
            markup = app_module._album_label(match)
            if row._diagnosis_markup != markup:
                row.query_one(Label).update(markup)
                row._diagnosis_markup = markup
        ranks = {match.album.cid: i for i, match in enumerate(matches)}
        if [item.album_cid for item in view.children] != list(ranks):
            view.sort_children(key=lambda row: ranks[row.album_cid])
        if matches:
            view.index = next((i for i, m in enumerate(matches) if m.album.cid == target_cid), 0)
            screen.current_album_cid = matches[view.index].album.cid
            if not search_has_focus:
                view.focus()
        else:
            screen.current_album_cid = None
        suffix = f"；搜索结果 {len(matches)} 张专辑" if screen.query_text else ""
        screen.query_one("#catalog-status", Static).update(
            f"已选择 {len(selection.selected)} 首歌曲{suffix}"
            if matches
            else f"没有匹配结果；已选择 {len(selection.selected)} 首歌曲"
        )

    async def timed_refresh(self, *, target_cid: str | None = None) -> None:
        screen = self
        caller = sys._getframe(1).f_code.co_name
        started = perf_counter()
        try:
            if variant == "once" and phase == "startup" and caller == "on_screen_resume":
                return
            if "reuse" in variant:
                return await reuse_refresh(screen, target_cid=target_cid)
            return await original(screen, target_cid=target_cid)
        finally:
            refreshes.append(
                {
                    "phase": phase,
                    "caller": caller,
                    "query": screen.query_text,
                    "ms": round((perf_counter() - started) * 1000, 2),
                }
            )

    CatalogScreen.refresh_albums = timed_refresh
    details = tuple(
        AlbumDetail(a.cid, a.name, "", a.cover_url, catalog.songs_by_album.get(a.cid, ()))
        for a in catalog.albums
    )
    config = AppConfig(HERE / "unused-config.toml", HERE / "unused-downloads", False)
    app = MonsterSirenApp(config, catalog=catalog, album_details=details)
    lag = []
    active = True

    async def heartbeat():
        while active:
            before = perf_counter()
            await asyncio.sleep(0.01)
            lag.append(max(0, (perf_counter() - before - 0.01) * 1000))

    probe = asyncio.create_task(heartbeat())
    timings = {}
    profiler = cProfile.Profile() if profile else None
    try:
        started = perf_counter()
        if profiler:
            profiler.enable()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0)
            timings["startup_ms"] = round((perf_counter() - started) * 1000, 2)

            async def press(label, *keys):
                nonlocal phase
                phase = label
                before = perf_counter()
                await pilot.press(*keys)
                await pilot.pause(0)
                timings[label] = round((perf_counter() - before) * 1000, 2)
                # Check the experimental list refresh retains IDs, order and selection labels.
                if isinstance(app.screen, CatalogScreen):
                    assert app.selection is not None
                    expected = app.selection.album_matches(app.screen.query_text)
                    actual = app.screen.query_one("#albums", ListView).children
                    assert [row.album_cid for row in actual] == [m.album.cid for m in expected]
                    assert [row.query_one(Label).render().plain for row in actual] == [
                        app_module.Text.from_markup(app_module._album_label(m)).plain
                        for m in expected
                    ]

            await press("down_ms", "down")
            await press("select_album_ms", "space")
            await press("deselect_album_ms", "space")
            await press("cached_detail_ms", "right")
            await press("back_to_catalog_ms", "left")
            await press("open_search_ms", "/")
            await press("search_a_ms", "a")
            await press("clear_search_ms", "backspace")
            await press("close_search_ms", "escape")
    finally:
        if profiler:
            profiler.disable()
            profiler.dump_stats(str(HERE / f"profile-{len(catalog.albums)}.pstats"))
        active = False
        await probe
        CatalogScreen.refresh_albums = original
        patches.close()
    result = {
        "albums": len(catalog.albums),
        "songs": len(catalog.songs),
        "variant": variant,
        "suggestions": dict(suggestions),
        "timings": timings,
        "refreshes": refreshes,
        "lag_budget_exceeded": max(lag, default=0) > 100,
        "heartbeat_max_lag_ms": round(max(lag, default=0), 2),
        "heartbeat_p95_lag_ms": round(sorted(lag)[int(len(lag) * 0.95)], 2),
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--count", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument(
        "--variant", choices=("baseline", "hex", "once", "reuse", "hex-reuse"), default="baseline"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "executable": sys.executable,
                "textual": importlib.metadata.version("textual"),
                "package": importlib.metadata.version("MSRMusicTool"),
            }
        ),
        flush=True,
    )
    catalog = await load_catalog(args.live, args.count)
    results = [await run(catalog, args.profile, args.variant) for _ in range(args.repeat)]
    output = HERE / f"{args.variant}-{len(catalog.albums)}{'-profile' if args.profile else ''}.json"
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "median_timings": {
                    k: round(statistics.median(r["timings"][k] for r in results), 2)
                    for k in results[0]["timings"]
                }
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
