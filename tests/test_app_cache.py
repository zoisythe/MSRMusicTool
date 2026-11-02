from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from textual.widgets import Input, OptionList

import msr_music_tool.app as app_module
from msr_music_tool.app import AlbumScreen, CatalogScreen, MonsterSirenApp
from msr_music_tool.catalog import Catalog
from msr_music_tool.catalog_cache import CachedCatalog, load_catalog_cache, save_catalog_cache
from msr_music_tool.config import AppConfig
from msr_music_tool.models import AlbumDetail, AlbumSummary
from msr_music_tool.site import MonsterSirenClient


async def wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("TUI state did not settle")


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> list[httpx.AsyncClient]:
    created = []

    def factory():
        def unexpected(request):
            raise AssertionError(f"Unexpected network call: {request.url.path}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(unexpected))
        created.append(client)
        return client

    monkeypatch.setattr(app_module, "_http_client", factory)
    return created


def cached_app(tmp_path: Path, catalog: Catalog) -> MonsterSirenApp:
    path = tmp_path / "catalog.json"
    save_catalog_cache(path, CachedCatalog(catalog, datetime(2000, 1, 1, tzinfo=UTC)))
    return MonsterSirenApp(
        AppConfig(tmp_path / "config.toml", tmp_path / "out", False),
        cache_path=path,
    )


async def test_old_cache_remains_interactive_offline_and_manual_refresh_recovers(
    tmp_path,
    sample_catalog,
    monkeypatch,
    clients,
):
    release = asyncio.Event()
    calls = 0

    async def fetch(_self):
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()
            raise RuntimeError("offline")
        return sample_catalog.albums, sample_catalog.songs

    monkeypatch.setattr(MonsterSirenClient, "fetch_catalog", fetch)
    app = cached_app(tmp_path, sample_catalog)
    async with app.run_test() as pilot:
        await wait_until(lambda: isinstance(app.screen, CatalogScreen) and app._loading)
        await pilot.press("space")
        assert app.selection is not None
        assert app.selection.selected == {"s1", "s2"}
        release.set()
        await wait_until(lambda: not app._loading)
        assert "刷新失败" in app.catalog_status
        assert "2000-01-01" in app.catalog_status
        view = app.screen.query_one("#albums", OptionList)
        option = view.get_option("a1")
        await pilot.press("r")
        await wait_until(lambda: calls == 2 and not app._loading)
        assert "刷新失败" not in app.catalog_status
        assert view.get_option("a1") is option
        assert app.selection.selected == {"s1", "s2"}
        await pilot.press("/", "r")
        await pilot.pause(0.08)
        assert app.screen.query_one("#search", Input).value == "r"
        assert calls == 2
    assert len(clients) == 1 and clients[0].is_closed
    saved = load_catalog_cache(app.cache_path)
    assert saved is not None and saved.fetched_at.year > 2000


async def test_background_refresh_waits_for_return_and_reconciles_selection(
    tmp_path,
    sample_catalog,
    monkeypatch,
    clients,
):
    release = asyncio.Event()
    albums = (
        AlbumSummary("a1", "Changed", "https://cdn.test/new.jpg", ("MSR",), 0),
        *sample_catalog.albums[1:],
    )
    songs = tuple(song for song in sample_catalog.songs if song.cid != "s2")

    async def fetch(_self):
        await release.wait()
        return albums, songs

    monkeypatch.setattr(MonsterSirenClient, "fetch_catalog", fetch)
    app = cached_app(tmp_path, sample_catalog)
    async with app.run_test() as pilot:
        await wait_until(lambda: isinstance(app.screen, CatalogScreen) and app._loading)
        detail = AlbumDetail("a1", "Original", "", "", sample_catalog.songs[:2])
        app.album_cache["a1"] = detail
        await pilot.press("space", "right")
        await wait_until(lambda: isinstance(app.screen, AlbumScreen))
        release.set()
        await wait_until(lambda: not app._loading)
        assert app.catalog is not None and app.selection is not None
        assert app.catalog.albums == sample_catalog.albums
        assert app._pending_catalog is not None
        assert app.selection.selected == {"s1", "s2"}
        await pilot.press("left")
        await wait_until(lambda: app._pending_catalog is None)
        assert app.catalog.albums == albums
        assert app.selection.selected == {"s1"}
        assert "a1" not in app.album_cache
        view = app.screen.query_one("#albums", OptionList)
        assert view.highlighted_option is not None and view.highlighted_option.id == "a1"


async def test_corrupt_cache_fetches_live_and_save_failure_does_not_break_browsing(
    tmp_path,
    sample_catalog,
    monkeypatch,
    clients,
):
    async def fetch(_self):
        return sample_catalog.albums, sample_catalog.songs

    def fail_save(*_args):
        raise PermissionError("read-only cache")

    monkeypatch.setattr(MonsterSirenClient, "fetch_catalog", fetch)
    monkeypatch.setattr(app_module, "save_catalog_cache", fail_save)
    path = tmp_path / "broken.json"
    path.write_text("{bad", encoding="utf-8")
    app = MonsterSirenApp(AppConfig(tmp_path / "config", tmp_path / "out", False), cache_path=path)
    async with app.run_test() as pilot:
        await wait_until(lambda: isinstance(app.screen, CatalogScreen) and not app._loading)
        assert "缓存保存失败" in app.catalog_status
        await pilot.press("space")
        assert app.selection is not None and app.selection.selected == {"s1", "s2"}


async def test_injected_catalog_skips_disk_cache_and_automatic_refresh(
    tmp_path,
    sample_catalog,
    monkeypatch,
    clients,
):
    def unexpected(*_args):
        raise AssertionError("Injected catalogs must not bootstrap")

    monkeypatch.setattr(app_module, "load_catalog_cache", unexpected)
    monkeypatch.setattr(MonsterSirenClient, "fetch_catalog", unexpected)
    app = MonsterSirenApp(
        AppConfig(tmp_path / "config", tmp_path / "out", False),
        catalog=sample_catalog,
        cache_path=tmp_path / "cache.json",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, CatalogScreen)
        assert not clients
    assert not app.cache_path.exists()


async def test_manual_refresh_supersedes_an_older_deferred_snapshot(
    tmp_path,
    sample_catalog,
    monkeypatch,
    clients,
):
    calls = 0

    async def fetch(_self):
        nonlocal calls
        calls += 1
        return sample_catalog.albums, (
            sample_catalog.songs[1:] if calls == 1 else sample_catalog.songs
        )

    async def pending_detail(_cid):
        await asyncio.Event().wait()
        raise AssertionError("Detail should have been cancelled")

    monkeypatch.setattr(MonsterSirenClient, "fetch_catalog", fetch)
    app = MonsterSirenApp(
        AppConfig(tmp_path / "config", tmp_path / "out", False),
        catalog=sample_catalog,
        cache_path=tmp_path / "catalog.json",
    )
    monkeypatch.setattr(app, "get_album_detail", pending_detail)
    async with app.run_test() as pilot:
        await pilot.press("right", "r")
        await wait_until(lambda: calls == 1 and not app._loading)
        assert app._pending_catalog is not None
        await pilot.press("r")
        await wait_until(lambda: calls == 2 and not app._loading)
        await pilot.press("escape")
        assert app.catalog is sample_catalog
        assert app._pending_catalog is None
