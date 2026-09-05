from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from textual.widgets import Button, Input, Label, ListView

import msr_music_tool.app as app_module
from msr_music_tool.app import (
    AlbumListItem,
    AlbumScreen,
    CatalogScreen,
    ConfirmScreen,
    LoadingScreen,
    MonsterSirenApp,
    ResultScreen,
)
from msr_music_tool.catalog import Catalog
from msr_music_tool.config import AppConfig
from msr_music_tool.downloader import BatchResult, SongDownloadResult, SongOutcome
from msr_music_tool.models import AlbumDetail, AlbumSummary, SongSummary


def _app_fixture(tmp_path: Path) -> MonsterSirenApp:
    albums = (
        AlbumSummary("new", "New Album", "https://cdn.test/new.jpg", (), 0),
        AlbumSummary("old", "Old Album", "https://cdn.test/old.png", (), 1),
    )
    songs = (
        SongSummary("s1", "First Song", "new", ()),
        SongSummary("s2", "Second Song", "new", ()),
        SongSummary("s3", "Needle Track", "old", ()),
    )
    catalog = Catalog(albums, songs)
    details = (
        AlbumDetail("new", "New Album", "new intro", albums[0].cover_url, songs[:2]),
        AlbumDetail("old", "Old Album", "old intro", albums[1].cover_url, songs[2:]),
    )
    return MonsterSirenApp(
        AppConfig(tmp_path / "config.toml", tmp_path / "downloads", False),
        catalog=catalog,
        album_details=details,
    )


@pytest.mark.asyncio
async def test_http_client_supports_socks_proxy_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("all_proxy", "socks5://127.0.0.1:1080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    async with app_module._http_client() as client:
        assert isinstance(client, httpx.AsyncClient)


@pytest.mark.asyncio
async def test_loading_error_buttons_are_keyboard_selectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient:
        async def __aenter__(self) -> object:
            raise RuntimeError("index failed")

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(app_module, "_http_client", FakeClient)
    app = MonsterSirenApp(AppConfig(tmp_path / "config.toml", tmp_path / "downloads", False))

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, LoadingScreen)
        retry = app.screen.query_one("#retry", Button)
        exit_button = app.screen.query_one("#exit", Button)
        assert retry.variant == exit_button.variant == "default"
        assert retry.has_focus
        unfocused_style = exit_button.rich_style
        focused_style = retry.rich_style
        assert focused_style != unfocused_style

        await pilot.press("right")
        assert exit_button.has_focus
        assert exit_button.rich_style == focused_style
        assert retry.rich_style == unfocused_style

        await pilot.press("left")
        assert retry.has_focus
        await pilot.press("down")
        assert exit_button.has_focus
        await pilot.press("up")
        assert retry.has_focus
        await pilot.press("right", "enter")
        await pilot.pause()
        assert not app.is_running


@pytest.mark.asyncio
async def test_selection_survives_detail_navigation_and_confirm_cancel(tmp_path: Path) -> None:
    app = _app_fixture(tmp_path)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, CatalogScreen)

        await pilot.press("space")
        await pilot.pause()
        assert app.selection is not None
        assert app.selection.selected == {"s1", "s2"}

        await pilot.press("right")
        await pilot.pause()
        assert isinstance(app.screen, AlbumScreen)

        await pilot.press("space")
        await pilot.pause()
        assert app.selection.selected == {"s2"}

        await pilot.press("left")
        await pilot.pause()
        assert isinstance(app.screen, CatalogScreen)
        assert app.selection.selected == {"s2"}

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert [song.cid for song in app.prepared_downloads[0].songs] == ["s2"]

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, CatalogScreen)
        assert app.selection.selected == {"s2"}


@pytest.mark.asyncio
async def test_search_filters_by_song_and_escape_restores_catalog(tmp_path: Path) -> None:
    app = _app_fixture(tmp_path)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("/")
        await pilot.press(*"needle")
        await pilot.pause()

        search = app.screen.query_one("#search", Input)
        albums = app.screen.query_one("#albums", ListView)
        assert search.display
        assert search.value == "needle"
        assert len(albums.children) == 1
        assert isinstance(albums.children[0], AlbumListItem)
        assert albums.children[0].album_cid == "old"

        await pilot.press("escape")
        await pilot.pause()
        assert not search.display
        assert len(albums.children) == 2

        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, CatalogScreen)


@pytest.mark.asyncio
async def test_album_indicators_use_requested_glyphs_and_colors(tmp_path: Path) -> None:
    app = _app_fixture(tmp_path)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        albums = app.screen.query_one("#albums", ListView)
        first = albums.children[0]
        assert isinstance(first, AlbumListItem)
        label = first.query_one(Label)
        rendered = label.render()
        assert rendered.plain.startswith("○")
        assert any(str(span.style) == "grey50" for span in rendered.spans)

        await pilot.press("space")
        await pilot.pause()
        first = albums.children[0]
        rendered = first.query_one(Label).render()
        assert rendered.plain.startswith("●")
        assert any(str(span.style) == "green" for span in rendered.spans)

        await pilot.press("right")
        await pilot.pause()
        await pilot.press("space")
        await pilot.press("left")
        await pilot.pause()
        first = albums.children[0]
        rendered = first.query_one(Label).render()
        assert rendered.plain.startswith("◐")
        assert any(str(span.style) == "yellow" for span in rendered.spans)


@pytest.mark.asyncio
async def test_completed_download_keeps_resize_and_result_keys_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app_fixture(tmp_path)
    assert app.catalog is not None
    downloaded_song = app.catalog.songs[0]

    class FakeClient:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeDownloader:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def execute(self, *_args: object, **_kwargs: object) -> BatchResult:
            return BatchResult((SongDownloadResult(downloaded_song, SongOutcome.SUCCESS, ()),))

    monkeypatch.setattr(app_module, "_http_client", FakeClient)
    monkeypatch.setattr(app_module, "BatchDownloader", FakeDownloader)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("space")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ResultScreen)

        await pilot.resize_terminal(60, 18)
        await pilot.pause()
        assert app.size == (60, 18)
        assert app.screen.size == (60, 18)
        back_button = app.screen.query_one("#back", Button)
        quit_button = app.screen.query_one("#quit", Button)
        assert back_button.region.bottom <= app.size.height
        assert quit_button.region.bottom <= app.size.height

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, CatalogScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["q", "ctrl+c"])
async def test_result_q_and_ctrl_c_exit(tmp_path: Path, key: str) -> None:
    app = _app_fixture(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await app.push_screen(ResultScreen(BatchResult(())))
        await pilot.pause()

        await pilot.press(key)
        await pilot.pause()
        assert not app.is_running


@pytest.mark.asyncio
async def test_confirm_buttons_are_neutral_and_keyboard_selectable(tmp_path: Path) -> None:
    app = _app_fixture(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("space", "enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)

        cancel = app.screen.query_one("#cancel", Button)
        confirm = app.screen.query_one("#confirm", Button)
        assert cancel.variant == confirm.variant == "default"
        assert confirm.has_focus
        unfocused_style = cancel.rich_style
        focused_style = confirm.rich_style
        assert focused_style != unfocused_style

        await pilot.press("up")
        assert cancel.has_focus
        assert cancel.rich_style == focused_style
        assert confirm.rich_style == unfocused_style

        await pilot.press("down")
        assert confirm.has_focus
        await pilot.press("left")
        assert cancel.has_focus
        await pilot.press("right")
        assert confirm.has_focus
        await pilot.press("left", "enter")
        await pilot.pause()

        assert isinstance(app.screen, CatalogScreen)
        assert app.selection is not None
        assert app.selection.selected == {"s1", "s2"}


@pytest.mark.asyncio
async def test_result_buttons_are_neutral_and_keyboard_selectable(tmp_path: Path) -> None:
    app = _app_fixture(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await app.push_screen(ResultScreen(BatchResult(())))
        await pilot.pause()

        back = app.screen.query_one("#back", Button)
        quit_button = app.screen.query_one("#quit", Button)
        assert back.variant == quit_button.variant == "default"
        assert back.has_focus
        unfocused_style = quit_button.rich_style
        focused_style = back.rich_style
        assert focused_style != unfocused_style

        await pilot.press("down")
        assert quit_button.has_focus
        assert quit_button.rich_style == focused_style
        assert back.rich_style == unfocused_style

        await pilot.press("up")
        assert back.has_focus
        await pilot.press("right")
        assert quit_button.has_focus
        await pilot.press("left", "enter")
        await pilot.pause()
        assert isinstance(app.screen, CatalogScreen)

        await app.push_screen(ResultScreen(BatchResult(())))
        await pilot.pause()
        await pilot.press("right", "enter")
        await pilot.pause()
        assert not app.is_running
