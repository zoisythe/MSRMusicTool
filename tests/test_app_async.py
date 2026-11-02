from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from textual import events
from textual.widgets import Input, OptionList

import msr_music_tool.app as app_module
from msr_music_tool.app import AlbumScreen, CatalogScreen, MonsterSirenApp
from msr_music_tool.catalog import Catalog
from msr_music_tool.catalog_cache import CachedCatalog
from msr_music_tool.config import AppConfig
from msr_music_tool.models import AlbumDetail
from msr_music_tool.site import MonsterSirenClient


async def wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("TUI state did not settle")


async def test_cancelled_detail_cannot_replace_a_newer_request(
    tmp_path,
    sample_catalog,
    monkeypatch,
):
    app = MonsterSirenApp(
        AppConfig(tmp_path / "config", tmp_path / "out", False),
        catalog=sample_catalog,
    )
    entered = {cid: asyncio.Event() for cid in ("a1", "a2")}
    release = {cid: asyncio.Event() for cid in ("a1", "a2")}

    async def detail(cid: str) -> AlbumDetail:
        entered[cid].set()
        try:
            await release[cid].wait()
        except asyncio.CancelledError:
            # Deliberately emulate a transport that delivers a result after cancellation.
            await release[cid].wait()
        return AlbumDetail(cid, cid, "", "", sample_catalog.songs_by_album[cid])

    monkeypatch.setattr(app, "get_album_detail", detail)
    async with app.run_test() as pilot:
        await pilot.press("right")
        await asyncio.wait_for(entered["a1"].wait(), 2)
        await pilot.press("down", "right")
        await asyncio.wait_for(entered["a2"].wait(), 2)
        release["a1"].set()
        await pilot.pause()
        assert isinstance(app.screen, CatalogScreen)
        release["a2"].set()
        await wait_until(lambda: isinstance(app.screen, AlbumScreen))
        assert isinstance(app.screen, AlbumScreen) and app.screen.album.cid == "a2"


async def test_pending_prepare_supports_escape_resize_and_ctrl_c(
    tmp_path,
    sample_catalog,
    monkeypatch,
):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    monkeypatch.setattr(app_module, "_http_client", lambda: client)
    app = MonsterSirenApp(
        AppConfig(tmp_path / "config", tmp_path / "out", False),
        catalog=sample_catalog,
    )
    entered = asyncio.Event()
    calls = []

    async def prepare(song_ids=None):
        assert song_ids is not None
        calls.append(set(song_ids))
        entered.set()
        await asyncio.Event().wait()
        return ()

    monkeypatch.setattr(app, "prepare_downloads", prepare)
    async with app.run_test() as pilot:
        assert await app.get_http_client() is client
        await pilot.press("space", "enter")
        await asyncio.wait_for(entered.wait(), 2)
        await pilot.press("enter")
        assert calls == [{"s1", "s2"}]
        await pilot.resize_terminal(50, 18)
        assert app.size == (50, 18)
        await pilot.press("escape")
        assert isinstance(app.screen, CatalogScreen) and not app.screen.operation_pending
        entered.clear()
        await pilot.press("enter")
        await asyncio.wait_for(entered.wait(), 2)
        await pilot.press("ctrl+c")
        assert app._quitting and not app.is_running
        assert app.prepared_downloads == ()
    assert client.is_closed
    await app.close_resources()


async def test_cancelled_client_waiter_does_not_cancel_initialization(
    tmp_path,
    sample_catalog,
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    created = []

    def factory():
        entered.set()
        if not release.wait(3):
            raise AssertionError("Client initialization was not released")
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
        created.append(client)
        return client

    monkeypatch.setattr(app_module, "_http_client", factory)
    app = MonsterSirenApp(
        AppConfig(tmp_path / "config", tmp_path / "out", False),
        catalog=sample_catalog,
    )
    async with app.run_test() as pilot:
        first = asyncio.create_task(app.get_http_client())
        await wait_until(entered.is_set)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(app.get_http_client())
        await pilot.resize_terminal(60, 20)
        release.set()
        client = await asyncio.wait_for(second, 2)
        assert client is await app.get_http_client()
        assert len(created) == 1
    assert created[0].is_closed
    await app.close_resources()


async def test_search_applies_only_latest_text_and_escape_cancels_debounce(
    tmp_path,
    sample_catalog,
):
    app = MonsterSirenApp(
        AppConfig(tmp_path / "config", tmp_path / "out", False),
        catalog=sample_catalog,
    )
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, CatalogScreen)
        screen.action_open_search()
        search = screen.query_one("#search", Input)
        search.value = "First"
        search.value = "Another"
        await pilot.pause(0.08)
        view = screen.query_one("#albums", OptionList)
        assert [option.id for option in view.options] == ["a2"]
        search.value = "unmatched"
        screen.action_cancel_or_exit()
        await pilot.pause(0.08)
        assert screen.query_text == "" and view.option_count == 3


async def test_late_detail_does_not_repopulate_replaced_catalog_cache(
    tmp_path,
    sample_catalog,
    monkeypatch,
):
    entered = asyncio.Event()
    release = asyncio.Event()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    monkeypatch.setattr(app_module, "_http_client", lambda: client)

    async def fetch(_self, cid):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return AlbumDetail(cid, "Old detail", "", "", sample_catalog.songs_by_album[cid])

    monkeypatch.setattr(MonsterSirenClient, "fetch_album", fetch)
    app = MonsterSirenApp(
        AppConfig(tmp_path / "config", tmp_path / "out", False),
        catalog=sample_catalog,
    )
    async with app.run_test() as pilot:
        await pilot.press("right")
        await asyncio.wait_for(entered.wait(), 2)
        changed = Catalog(sample_catalog.albums, sample_catalog.songs[1:])
        app._pending_catalog = CachedCatalog(changed, datetime.now(UTC))
        await pilot.press("escape")
        assert app.catalog is changed
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, CatalogScreen)
        assert "a1" not in app.album_cache


async def test_search_shortcut_routes_an_immediate_next_key_to_input(tmp_path, sample_catalog):
    app = MonsterSirenApp(
        AppConfig(tmp_path / "config", tmp_path / "out", False),
        catalog=sample_catalog,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        driver = app._driver
        assert driver is not None
        driver.send_message(events.Key("slash", "/"))
        driver.send_message(events.Key("a", "a"))
        search = app.screen.query_one("#search", Input)
        await wait_until(lambda: search.value == "a")
        await pilot.press("/")
        assert search.value == "a/"
