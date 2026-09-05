from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import httpx
from rich.markup import escape
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    ProgressBar,
    RichLog,
    Static,
)

from .catalog import Catalog, SelectionModel
from .config import AppConfig
from .downloader import (
    AssetOutcome,
    AssetResult,
    BatchDownloader,
    BatchResult,
    DownloadProgress,
    SongDownloadResult,
    SongOutcome,
)
from .models import AlbumDetail, AlbumDownload, AlbumMatch, SelectionState, SongSummary
from .naming import FileNamer
from .site import MonsterSirenClient

HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0, read=60.0, write=60.0, pool=10.0)
USER_AGENT = "MSRMusicTool/0.1 (+https://monster-siren.hypergryph.com/music)"


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


class AlbumListItem(ListItem):
    def __init__(self, match: AlbumMatch) -> None:
        self.album_cid = match.album.cid
        super().__init__(Label(_album_label(match)))


class SongListItem(ListItem):
    def __init__(self, song: SongSummary, selected: bool) -> None:
        self.song_cid = song.cid
        super().__init__(Label(_song_label(song, selected)))

    def set_selected(self, song: SongSummary, selected: bool) -> None:
        cast(Label, self.children[0]).update(_song_label(song, selected))


class LoadingScreen(Screen[None]):
    BINDINGS = [
        Binding("r", "retry", "重试", show=False),
        Binding("escape", "exit_app", "退出"),
        Binding("left", "focus_retry", "重试", show=False),
        Binding("up", "focus_retry", "重试", show=False),
        Binding("right", "focus_exit", "退出", show=False),
        Binding("down", "focus_exit", "退出", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static("正在读取塞壬唱片索引……", id="loading-message")
        with Horizontal(id="loading-actions"):
            yield Button("重试", id="retry")
            yield Button("退出", id="exit")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#loading-actions").display = False

    def set_loading(self) -> None:
        self.query_one("#loading-message", Static).update("正在读取塞壬唱片索引……")
        self.query_one("#loading-actions").display = False

    def set_error(self, error: Exception) -> None:
        self.query_one("#loading-message", Static).update(
            f"[red]索引加载失败[/red]\n{escape(str(error))}"
        )
        self.query_one("#loading-actions").display = True
        self.query_one("#retry", Button).focus()

    def action_retry(self) -> None:
        msr_app = cast(MonsterSirenApp, self.app)
        msr_app.retry_catalog()

    def action_exit_app(self) -> None:
        self.app.exit()

    def action_focus_retry(self) -> None:
        self.query_one("#retry", Button).focus()

    def action_focus_exit(self) -> None:
        self.query_one("#exit", Button).focus()

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        if event.button.id == "retry":
            self.action_retry()
        elif event.button.id == "exit":
            self.action_exit_app()


class CatalogScreen(Screen[None]):
    BINDINGS = [
        Binding("/", "open_search", "搜索"),
        Binding("space", "toggle_album", "选择专辑"),
        Binding("right", "open_detail", "专辑详情"),
        Binding("enter", "confirm", "确认下载", priority=True),
        Binding("escape", "cancel_or_exit", "退出"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.query_text = ""
        self.current_album_cid: str | None = None
        self._preparing = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(id="paths")
        yield Input(placeholder="输入专辑名或歌曲名；Esc 关闭搜索", id="search")
        yield ListView(id="albums")
        yield Static(id="catalog-status")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#search", Input).display = False
        self._update_paths()
        await self.refresh_albums()

    async def on_screen_resume(self, _: events.ScreenResume) -> None:
        await self.refresh_albums(target_cid=self.current_album_cid)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return not (
            isinstance(self.focused, Input)
            and action in {"open_search", "toggle_album", "open_detail", "confirm"}
        )

    @on(Input.Changed, "#search")
    async def search_changed(self, event: Input.Changed) -> None:
        self.query_text = event.value
        await self.refresh_albums(target_cid=self.current_album_cid)

    @on(ListView.Highlighted, "#albums")
    def album_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, AlbumListItem):
            self.current_album_cid = event.item.album_cid

    async def refresh_albums(self, *, target_cid: str | None = None) -> None:
        msr_app = cast(MonsterSirenApp, self.app)
        if msr_app.selection is None:
            return
        search = self.query_one("#search", Input)
        search_has_focus = search.display and self.focused is search
        view = self.query_one("#albums", ListView)
        if target_cid is None:
            item = _current_list_item(view)
            if isinstance(item, AlbumListItem):
                target_cid = item.album_cid
        matches = msr_app.selection.album_matches(self.query_text)
        await view.clear()
        if matches:
            await view.extend(AlbumListItem(match) for match in matches)
            index = next(
                (i for i, match in enumerate(matches) if match.album.cid == target_cid),
                0,
            )
            view.index = index
            self.current_album_cid = matches[index].album.cid
            if not search_has_focus:
                view.focus()
        else:
            self.current_album_cid = None
        selected = len(msr_app.selection.selected)
        suffix = f"；搜索结果 {len(matches)} 张专辑" if self.query_text else ""
        self.query_one("#catalog-status", Static).update(
            f"已选择 {selected} 首歌曲{suffix}"
            if matches
            else f"没有匹配结果；已选择 {selected} 首歌曲"
        )

    def action_open_search(self) -> None:
        search = self.query_one("#search", Input)
        search.display = True
        search.focus()

    async def action_cancel_or_exit(self) -> None:
        search = self.query_one("#search", Input)
        if search.display:
            had_query = bool(search.value)
            search.display = False
            search.value = ""
            self.query_text = ""
            if not had_query:
                await self.refresh_albums(target_cid=self.current_album_cid)
            self.query_one("#albums", ListView).focus()
            return
        self.app.exit()

    async def action_toggle_album(self) -> None:
        msr_app = cast(MonsterSirenApp, self.app)
        if msr_app.selection is None or self.current_album_cid is None:
            return
        target = self.current_album_cid
        msr_app.selection.toggle_album(target)
        await self.refresh_albums(target_cid=target)

    async def action_open_detail(self) -> None:
        if self.current_album_cid is None:
            return
        msr_app = cast(MonsterSirenApp, self.app)
        try:
            detail = await msr_app.get_album_detail(self.current_album_cid)
        except Exception as exc:
            self.notify(f"专辑详情加载失败：{exc}", severity="error", timeout=6)
            return
        await self.app.push_screen(AlbumScreen(detail))

    async def action_confirm(self) -> None:
        msr_app = cast(MonsterSirenApp, self.app)
        if msr_app.selection is None or not msr_app.selection.selected:
            self.notify("请先选择至少一首歌曲", severity="warning")
            return
        if self._preparing:
            return
        self._preparing = True
        self.query_one("#catalog-status", Static).update("正在整理已选歌曲……")
        try:
            msr_app.prepared_downloads = await msr_app.prepare_downloads()
            await self.app.push_screen(ConfirmScreen())
        except Exception as exc:
            self.notify(f"确认信息加载失败：{exc}", severity="error", timeout=8)
        finally:
            self._preparing = False
            if self.app.screen is self:
                await self.refresh_albums(target_cid=self.current_album_cid)

    def _update_paths(self) -> None:
        config = cast(MonsterSirenApp, self.app).config
        mode = "配置目录" if config.is_configured else "默认目录"
        self.query_one("#paths", Static).update(
            f"配置：{escape(str(config.config_path))}\n"
            f"下载：{escape(str(config.download_dir))}（{mode}）"
        )


class AlbumScreen(Screen[None]):
    BINDINGS = [
        Binding("space", "toggle_song", "选择歌曲"),
        Binding("left", "back", "返回专辑列表"),
    ]

    def __init__(self, album: AlbumDetail) -> None:
        super().__init__()
        self.album = album

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(f"专辑：{escape(self.album.name.strip())}", classes="screen-title")
        yield ListView(id="songs")
        yield Static(id="album-status")
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_songs()

    async def refresh_songs(self, *, target_cid: str | None = None) -> None:
        selection = cast(MonsterSirenApp, self.app).selection
        if selection is None:
            return
        view = self.query_one("#songs", ListView)
        if target_cid is None:
            item = _current_list_item(view)
            if isinstance(item, SongListItem):
                target_cid = item.song_cid
        await view.clear()
        await view.extend(
            SongListItem(song, song.cid in selection.selected) for song in self.album.songs
        )
        if self.album.songs:
            index = next(
                (i for i, song in enumerate(self.album.songs) if song.cid == target_cid),
                0,
            )
            view.index = index
            view.focus()
        selected_count = sum(song.cid in selection.selected for song in self.album.songs)
        self.query_one("#album-status", Static).update(
            f"本专辑已选择 {selected_count}/{len(self.album.songs)} 首；Left 返回"
        )

    async def action_toggle_song(self) -> None:
        view = self.query_one("#songs", ListView)
        item = _current_list_item(view)
        if not isinstance(item, SongListItem):
            return
        selection = cast(MonsterSirenApp, self.app).selection
        if selection is None:
            return
        target = item.song_cid
        selection.toggle_song(target)
        await self.refresh_songs(target_cid=target)

    def action_back(self) -> None:
        self.app.pop_screen()


class ConfirmScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("left", "focus_cancel", "取消", show=False),
        Binding("up", "focus_cancel", "取消", show=False),
        Binding("right", "focus_confirm", "确认", show=False),
        Binding("down", "focus_confirm", "确认", show=False),
    ]

    def compose(self) -> ComposeResult:
        msr_app = cast(MonsterSirenApp, self.app)
        yield Header(show_clock=False)
        yield Static("确认下载", classes="screen-title")
        with VerticalScroll(id="confirmation-list"):
            yield Static(_confirmation_markup(msr_app.prepared_downloads))
        with Horizontal(classes="button-row"):
            yield Button("取消", id="cancel")
            yield Button("确认下载", id="confirm")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#confirm", Button).focus()

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def action_focus_cancel(self) -> None:
        self.query_one("#cancel", Button).focus()

    def action_focus_confirm(self) -> None:
        self.query_one("#confirm", Button).focus()

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "confirm":
            self.app.switch_screen(DownloadScreen())


class DownloadScreen(Screen[None]):
    class Finished(Message):
        def __init__(self, result: BatchResult) -> None:
            super().__init__()
            self.result = result

    def compose(self) -> ComposeResult:
        total = sum(len(item.songs) for item in cast(MonsterSirenApp, self.app).prepared_downloads)
        yield Header(show_clock=False)
        yield Static("正在下载", classes="screen-title")
        yield ProgressBar(total=total, id="download-progress")
        yield Static(f"0/{total}", id="download-status")
        yield RichLog(id="download-log", wrap=True, markup=True)

    def on_mount(self) -> None:
        self.app.call_after_refresh(self._start_download)

    def _start_download(self) -> None:
        self.app.run_worker(self._run_download(), exclusive=True, name="download-batch")

    async def _run_download(self) -> None:
        msr_app = cast(MonsterSirenApp, self.app)
        try:
            async with _http_client() as http:
                site = MonsterSirenClient(http)
                downloader = BatchDownloader(
                    site,
                    http,
                    msr_app.config.download_dir,
                    cast(FileNamer, msr_app.namer),
                )
                result = await downloader.execute(
                    msr_app.prepared_downloads,
                    progress=self._update_progress,
                )
        except Exception as exc:
            result = _fatal_batch_result(
                msr_app.prepared_downloads, msr_app.config.download_dir, exc
            )
        self.post_message(self.Finished(result))

    @on(Finished)
    def handle_finished(self, event: Finished) -> None:
        msr_app = cast(MonsterSirenApp, self.app)
        result = event.result
        msr_app.last_result = result
        if msr_app.selection is not None:
            msr_app.selection.clear_completed(result.successful_ids)
        self.app.switch_screen(ResultScreen(result))

    def _update_progress(self, update: DownloadProgress) -> None:
        self.query_one("#download-progress", ProgressBar).update(progress=update.completed)
        outcome = _outcome_text(update.outcome)
        self.query_one("#download-status", Static).update(
            f"{update.completed}/{update.total}  {escape(update.song.name)}：{outcome}"
        )
        self.query_one("#download-log", RichLog).write(
            f"{_outcome_icon(update.outcome)} {escape(update.song.name)}  {outcome}"
        )


class ResultScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "back_to_catalog", "返回主界面"),
        Binding("q", "quit_app", "退出"),
        Binding("left", "focus_back", "返回", show=False),
        Binding("up", "focus_back", "返回", show=False),
        Binding("right", "focus_quit", "退出", show=False),
        Binding("down", "focus_quit", "退出", show=False),
    ]

    def __init__(self, result: BatchResult) -> None:
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("下载结果", classes="screen-title")
        with VerticalScroll(id="result-list"):
            yield Static(_result_markup(self.result))
        with Horizontal(classes="button-row"):
            yield Button("返回主界面 (Esc)", id="back")
            yield Button("退出 (q)", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#back", Button).focus()

    def action_back_to_catalog(self) -> None:
        self.app.pop_screen()

    def action_quit_app(self) -> None:
        self.app.exit()

    def action_focus_back(self) -> None:
        self.query_one("#back", Button).focus()

    def action_focus_quit(self) -> None:
        self.query_one("#quit", Button).focus()

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.action_back_to_catalog()
        elif event.button.id == "quit":
            self.action_quit_app()


class MonsterSirenApp(App[None]):
    TITLE = "MSRMusicTool"
    SUB_TITLE = "塞壬唱片音乐下载器"
    CSS_PATH = "msr_music_tool.tcss"
    BINDINGS = [
        Binding(
            "ctrl+c",
            "force_quit",
            show=False,
            priority=True,
            system=True,
        )
    ]

    def __init__(
        self,
        config: AppConfig,
        *,
        catalog: Catalog | None = None,
        album_details: Iterable[AlbumDetail] = (),
    ) -> None:
        super().__init__()
        self.config = config
        self.catalog = catalog
        self.selection = SelectionModel(catalog) if catalog else None
        self.namer = FileNamer(catalog) if catalog else None
        self.album_cache = {album.cid: album for album in album_details}
        self.prepared_downloads: tuple[AlbumDownload, ...] = ()
        self.last_result: BatchResult | None = None
        self._loading = False

    def action_force_quit(self) -> None:
        self.exit()

    async def on_mount(self) -> None:
        if self.catalog is not None:
            await self.push_screen(CatalogScreen())
        else:
            await self.push_screen(LoadingScreen())
            self.retry_catalog()

    def retry_catalog(self) -> None:
        if self._loading:
            return
        if isinstance(self.screen, LoadingScreen):
            self.screen.set_loading()
        self._loading = True
        self.run_worker(self._load_catalog(), exclusive=True, name="load-catalog")

    async def _load_catalog(self) -> None:
        try:
            async with _http_client() as http:
                albums, songs = await MonsterSirenClient(http).fetch_catalog()
            self.catalog = Catalog(albums, songs)
            self.selection = SelectionModel(self.catalog)
            self.namer = FileNamer(self.catalog)
            await self.switch_screen(CatalogScreen())
        except Exception as exc:
            if isinstance(self.screen, LoadingScreen):
                self.screen.set_error(exc)
        finally:
            self._loading = False

    async def get_album_detail(self, cid: str) -> AlbumDetail:
        cached = self.album_cache.get(cid)
        if cached is not None:
            return cached
        async with _http_client() as http:
            detail = await MonsterSirenClient(http).fetch_album(cid)
        self.album_cache[cid] = detail
        return detail

    async def prepare_downloads(self) -> tuple[AlbumDownload, ...]:
        if self.catalog is None or self.selection is None:
            return ()
        album_cids = [
            album.cid
            for album in self.catalog.albums
            if any(
                song.cid in self.selection.selected
                for song in self.catalog.songs_by_album.get(album.cid, ())
            )
        ]
        semaphore = asyncio.Semaphore(4)

        async def fetch(cid: str) -> AlbumDetail:
            async with semaphore:
                return await self.get_album_detail(cid)

        details = await asyncio.gather(*(fetch(cid) for cid in album_cids))
        prepared: list[AlbumDownload] = []
        found: set[str] = set()
        for detail in details:
            songs = tuple(song for song in detail.songs if song.cid in self.selection.selected)
            found.update(song.cid for song in songs)
            if songs:
                prepared.append(AlbumDownload(detail, songs))
        missing = self.selection.selected - found
        if missing:
            raise RuntimeError(f"官网详情中缺少 {len(missing)} 首已选歌曲，请刷新索引后重试")
        return tuple(prepared)


def _current_list_item(view: ListView) -> ListItem | None:
    index = view.index
    if index is None or index < 0 or index >= len(view.children):
        return None
    child = view.children[index]
    return child if isinstance(child, ListItem) else None


def _album_label(match: AlbumMatch) -> str:
    indicator = {
        SelectionState.FULL: "[green]●[/green]",
        SelectionState.PARTIAL: "[yellow]◐[/yellow]",
        SelectionState.NONE: "[grey50]○[/grey50]",
    }[match.state]
    text = f"{indicator}  {escape(match.album.name.strip())}"
    if match.matched_songs:
        shown = "、".join(escape(song.name) for song in match.matched_songs[:3])
        extra = len(match.matched_songs) - 3
        text += f"\n    [dim]匹配曲目：{shown}{f' 等 {extra + 3} 首' if extra else ''}[/dim]"
    return text


def _song_label(song: SongSummary, selected: bool) -> str:
    indicator = "[green]●[/green]" if selected else "[grey50]○[/grey50]"
    artists = " / ".join(part.strip() for part in song.artists if part.strip())
    suffix = f"  [dim]{escape(artists)}[/dim]" if artists else ""
    return f"{indicator}  {escape(song.name)}{suffix}"


def _confirmation_markup(items: Iterable[AlbumDownload]) -> str:
    lines: list[str] = []
    count = 0
    for item in items:
        lines.append(f"[bold]{escape(item.album.name.strip())}[/bold]")
        for song in item.songs:
            count += 1
            lines.append(f"  • {escape(song.name)}")
        lines.append("")
    return f"共 {count} 首歌曲\n\n" + "\n".join(lines)


def _result_markup(result: BatchResult) -> str:
    success = result.count(SongOutcome.SUCCESS)
    skipped = result.count(SongOutcome.SKIPPED)
    failed = result.count(SongOutcome.FAILED)
    lines = [f"成功 {success} 首 · 已跳过 {skipped} 首 · 失败 {failed} 首", ""]
    for item in result.songs:
        lines.append(
            f"{_outcome_icon(item.outcome)} {escape(item.song.name)}  {_outcome_text(item.outcome)}"
        )
        for error in item.errors:
            lines.append(f"    [red]{escape(error)}[/red]")
    if failed:
        lines.extend(["", "失败歌曲会继续保持选中，返回主界面后可重新确认下载。"])
    return "\n".join(lines)


def _outcome_text(outcome: SongOutcome) -> str:
    return {
        SongOutcome.SUCCESS: "成功",
        SongOutcome.SKIPPED: "已存在，跳过",
        SongOutcome.FAILED: "失败",
    }[outcome]


def _outcome_icon(outcome: SongOutcome) -> str:
    return {
        SongOutcome.SUCCESS: "[green]✓[/green]",
        SongOutcome.SKIPPED: "[cyan]↷[/cyan]",
        SongOutcome.FAILED: "[red]✗[/red]",
    }[outcome]


def _fatal_batch_result(
    items: Iterable[AlbumDownload], output_dir: Path, error: Exception
) -> BatchResult:
    results = []
    for item in items:
        for song in item.songs:
            asset = AssetResult("批次", AssetOutcome.FAILED, output_dir, str(error))
            results.append(SongDownloadResult(song, SongOutcome.FAILED, (asset,)))
    return BatchResult(tuple(results))
