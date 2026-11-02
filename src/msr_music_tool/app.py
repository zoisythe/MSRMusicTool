from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
from rich.markup import escape
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    OptionList,
    RichLog,
    Select,
    Static,
)
from textual.widgets.option_list import Option
from textual.worker import Worker

from .catalog import Catalog, SelectionModel
from .catalog_cache import (
    CachedCatalog,
    default_cache_path,
    load_catalog_cache,
    save_catalog_cache,
)
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
from .models import (
    AlbumDetail,
    AlbumDownload,
    AlbumMatch,
    DownloadMode,
    SelectionState,
    SongSummary,
)
from .naming import FileNamer
from .progress import ProgressState
from .service import http_client, prepare_downloads
from .site import MonsterSirenClient
from .transfers import ResourceProgress


def _http_client() -> httpx.AsyncClient:
    return http_client()


class CatalogOptions(OptionList):
    """CID-addressed rows with no per-row widgets or unchanged-row rendering."""

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self._row_labels: dict[str, str] = {}

    @property
    def current_cid(self) -> str | None:
        option = self.highlighted_option
        return option.id if option is not None else None

    @staticmethod
    def _row_text(label: str) -> Text:
        # OptionList 8.2 counts content lines but not component vertical padding.
        # Put the spacing in the content so its virtual height matches its rendering.
        return Text("\n").append_text(Text.from_markup(label)).append("\n")

    def update_rows(self, rows: Iterable[tuple[str, str]], target_cid: str | None) -> None:
        labels = dict(rows)
        old_ids = tuple(self._row_labels)
        new_ids = tuple(labels)
        target_cid = target_cid or self.current_cid
        changed = labels != self._row_labels or old_ids != new_ids
        with self.prevent(OptionList.OptionHighlighted), self.app.batch_update():
            if old_ids != new_ids:
                existing = {option.id: option for option in self.options}
                self.set_options(
                    existing[cid]
                    if cid in existing and self._row_labels[cid] == label
                    else Option(self._row_text(label), id=cid)
                    for cid, label in labels.items()
                )
            elif changed:
                for cid, label in labels.items():
                    if self._row_labels[cid] != label:
                        self.replace_option_prompt(cid, self._row_text(label))
            self._row_labels = labels
            index = new_ids.index(target_cid) if target_cid in labels else (0 if labels else None)
            if self.highlighted != index:
                self.highlighted = index
            if changed and index is not None:
                self.call_after_refresh(self.scroll_to_highlight)


class LoadingScreen(Screen[None]):
    AUTO_FOCUS = ""

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

    def show_loading_message(self) -> None:
        self.query_one("#loading-message", Static).update("正在读取塞壬唱片索引……")
        self.query_one("#loading-actions").display = False

    def set_error(self, error: Exception) -> None:
        self.query_one("#loading-message", Static).update(
            f"[red]索引加载失败[/red]\n{escape(str(error))}"
        )
        self.query_one("#loading-actions").display = True
        self.set_focus(self.query_one("#retry", Button))

    def action_retry(self) -> None:
        msr_app = cast(MonsterSirenApp, self.app)
        msr_app.retry_catalog()

    def action_exit_app(self) -> None:
        self.app.exit()

    def action_focus_retry(self) -> None:
        self.set_focus(self.query_one("#retry", Button))

    def action_focus_exit(self) -> None:
        self.set_focus(self.query_one("#exit", Button))

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        if event.button.id == "retry":
            self.action_retry()
        elif event.button.id == "exit":
            self.action_exit_app()


class CatalogScreen(Screen[None]):
    BINDINGS = [
        Binding("/", "open_search", "搜索", priority=True),
        Binding("space", "toggle_album", "选择专辑"),
        Binding("right", "open_detail", "专辑详情"),
        Binding("enter", "confirm", "确认下载", priority=True),
        Binding("escape", "cancel_or_exit", "退出"),
        Binding("r", "refresh_catalog", "刷新曲库"),
    ]

    class OperationFinished(Message):
        def __init__(
            self,
            generation: int,
            result: AlbumDetail | tuple[AlbumDownload, ...] | Exception,
        ) -> None:
            super().__init__()
            self.generation = generation
            self.result = result

    def __init__(self) -> None:
        super().__init__()
        self.query_text = ""
        self.current_album_cid: str | None = None
        self._initialized = False
        self._has_resumed = False
        self._search_timer: Timer | None = None
        self._operation_generation = 0
        self._operation_worker: Worker[None] | None = None
        self._operation_kind: str | None = None

    @property
    def operation_pending(self) -> bool:
        return self._operation_kind is not None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(id="paths")
        yield Input(placeholder="输入专辑名或歌曲名；Esc 关闭搜索", id="search")
        yield CatalogOptions(id="albums")
        yield Static(id="catalog-status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#search", Input).display = False
        self._update_paths()
        self.refresh_albums()
        self._initialized = True

    def on_screen_resume(self, _: events.ScreenResume) -> None:
        if not self._has_resumed:
            self._has_resumed = True
            return
        if self._initialized:
            msr_app = cast(MonsterSirenApp, self.app)
            if not msr_app.apply_pending_catalog():
                self.refresh_albums(target_cid=self.current_album_cid)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return not (
            isinstance(self.focused, Input)
            and action
            in {"open_search", "toggle_album", "open_detail", "confirm", "refresh_catalog"}
        )

    @on(Input.Changed, "#search")
    def search_changed(self, event: Input.Changed) -> None:
        if not event.input.display or event.value == self.query_text:
            return
        self.cancel_operation()
        self._stop_search_timer()
        self._search_timer = self.set_timer(0.06, self._apply_search)

    def _stop_search_timer(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None

    def _apply_search(self) -> None:
        self._search_timer = None
        self.query_text = self.query_one("#search", Input).value
        self.refresh_albums(target_cid=self.current_album_cid)

    @on(OptionList.OptionHighlighted, "#albums")
    def album_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.current_album_cid != event.option.id:
            self.current_album_cid = event.option.id
            self.cancel_operation()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if event.widget.id != "albums":
            self.cancel_operation()

    def refresh_albums(self, *, target_cid: str | None = None) -> None:
        msr_app = cast(MonsterSirenApp, self.app)
        if msr_app.selection is None:
            return
        search = self.query_one("#search", Input)
        search_has_focus = search.display and self.focused is search
        view = self.query_one("#albums", CatalogOptions)
        matches = msr_app.selection.album_matches(self.query_text)
        view.update_rows(
            ((match.album.cid, _album_label(match)) for match in matches),
            target_cid or self.current_album_cid,
        )
        self.current_album_cid = view.current_cid
        if matches and not search_has_focus:
            view.focus()
        self.refresh_status()

    def refresh_status(self) -> None:
        msr_app = cast(MonsterSirenApp, self.app)
        if msr_app.selection is None:
            return
        count = self.query_one("#albums", CatalogOptions).option_count
        selected = len(msr_app.selection.selected)
        suffix = f"；搜索结果 {count} 张专辑" if self.query_text else ""
        text = (
            f"已选择 {selected} 首歌曲{suffix}"
            if count
            else f"没有匹配结果；已选择 {selected} 首歌曲"
        )
        if msr_app.catalog_status:
            text += f"\n{msr_app.catalog_status}"
        if self._operation_kind:
            operation = (
                "正在读取专辑详情" if self._operation_kind == "detail" else "正在整理已选歌曲"
            )
            text += f"\n{operation}……（Esc 取消）"
        self.query_one("#catalog-status", Static).update(Text(text))

    def action_open_search(self) -> None:
        self.cancel_operation()
        search = self.query_one("#search", Input)
        search.display = True
        self.set_focus(search)

    def action_cancel_or_exit(self) -> None:
        if self.operation_pending:
            self.cancel_operation()
            return
        search = self.query_one("#search", Input)
        if search.display:
            self._stop_search_timer()
            search.display = False
            with search.prevent(Input.Changed):
                search.value = ""
            self.query_text = ""
            self.refresh_albums(target_cid=self.current_album_cid)
            self.query_one("#albums", CatalogOptions).focus()
            return
        self.app.exit()

    def action_toggle_album(self) -> None:
        msr_app = cast(MonsterSirenApp, self.app)
        if msr_app.selection is None or self.current_album_cid is None:
            return
        target = self.current_album_cid
        self.cancel_operation(apply_pending=False)
        msr_app.selection.toggle_album(target)
        if not msr_app.apply_pending_catalog():
            self.refresh_albums(target_cid=target)

    def action_open_detail(self) -> None:
        if self.current_album_cid is None:
            return
        self._start_operation("detail")

    def action_confirm(self) -> None:
        msr_app = cast(MonsterSirenApp, self.app)
        if msr_app.selection is None or not msr_app.selection.selected:
            self.notify("请先选择至少一首歌曲", severity="warning")
            return
        if self._operation_kind == "prepare":
            return
        self._start_operation("prepare")

    def action_refresh_catalog(self) -> None:
        cast(MonsterSirenApp, self.app).retry_catalog()

    def cancel_operation(self, *, apply_pending: bool = True) -> None:
        if not self.operation_pending:
            return
        self._operation_generation += 1
        if self._operation_worker is not None:
            self._operation_worker.cancel()
        self._operation_worker = None
        self._operation_kind = None
        if self._initialized:
            self.refresh_status()
            if apply_pending:
                cast(MonsterSirenApp, self.app).apply_pending_catalog()

    def _start_operation(self, kind: str) -> None:
        self.cancel_operation(apply_pending=False)
        self._operation_generation += 1
        self._operation_kind = kind
        app = cast(MonsterSirenApp, self.app)
        selected = tuple(app.selection.selected) if app.selection is not None else ()
        self._operation_worker = self.run_worker(
            self._run_operation(self._operation_generation, kind, self.current_album_cid, selected),
            group="catalog-operation",
            exclusive=True,
            name=kind,
        )
        self.refresh_status()

    async def _run_operation(
        self,
        generation: int,
        kind: str,
        cid: str | None,
        selected: tuple[str, ...],
    ) -> None:
        app = cast(MonsterSirenApp, self.app)
        result: AlbumDetail | tuple[AlbumDownload, ...]
        try:
            if kind == "detail":
                assert cid is not None
                result = await app.get_album_detail(cid)
            else:
                result = await app.prepare_downloads(selected)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.post_message(self.OperationFinished(generation, exc))
        else:
            self.post_message(self.OperationFinished(generation, result))

    @on(OperationFinished)
    async def operation_finished(self, event: OperationFinished) -> None:
        app = cast(MonsterSirenApp, self.app)
        if event.generation != self._operation_generation or app.screen is not self or app.closing:
            return
        self._operation_kind = None
        self._operation_worker = None
        self.refresh_status()
        if isinstance(event.result, Exception):
            self.notify(f"信息加载失败：{event.result}", severity="error", timeout=6)
            app.apply_pending_catalog()
        elif isinstance(event.result, AlbumDetail):
            await app.push_screen(AlbumScreen(event.result))
        else:
            app.prepared_downloads = event.result
            await app.push_screen(ConfirmScreen())

    def on_unmount(self) -> None:
        self._initialized = False
        self._stop_search_timer()
        self.cancel_operation(apply_pending=False)

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
        yield CatalogOptions(id="songs")
        yield Static(id="album-status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_songs()

    def refresh_songs(self, *, target_cid: str | None = None) -> None:
        selection = cast(MonsterSirenApp, self.app).selection
        if selection is None:
            return
        view = self.query_one("#songs", CatalogOptions)
        view.update_rows(
            (
                (song.cid, _song_label(song, song.cid in selection.selected))
                for song in self.album.songs
            ),
            target_cid,
        )
        if self.album.songs:
            view.focus()
        selected_count = sum(song.cid in selection.selected for song in self.album.songs)
        self.query_one("#album-status", Static).update(
            f"本专辑已选择 {selected_count}/{len(self.album.songs)} 首；Left 返回"
        )

    def action_toggle_song(self) -> None:
        view = self.query_one("#songs", CatalogOptions)
        target = view.current_cid
        if target is None:
            return
        selection = cast(MonsterSirenApp, self.app).selection
        if selection is None:
            return
        selection.toggle_song(target)
        self.refresh_songs(target_cid=target)

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
        yield Select(
            [
                ("全部：音频、歌词、封面", "all"),
                ("仅音频", "audio"),
                ("音频与歌词", "audio-lyrics"),
            ],
            value=msr_app.download_mode.value,
            allow_blank=False,
            id="download-mode",
        )
        with Horizontal(classes="button-row"):
            yield Button("取消", id="cancel")
            yield Button("确认下载", id="confirm")
        yield Footer()

    def on_mount(self) -> None:
        self.set_focus(self.query_one("#confirm", Button))

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def action_focus_cancel(self) -> None:
        self.set_focus(self.query_one("#cancel", Button))

    def action_focus_confirm(self) -> None:
        self.set_focus(self.query_one("#confirm", Button))

    @on(Button.Pressed)
    def handle_button(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "confirm":
            value = self.query_one("#download-mode", Select).value
            cast(MonsterSirenApp, self.app).download_mode = DownloadMode(str(value))
            self.app.switch_screen(DownloadScreen())


class DownloadScreen(Screen[None]):
    class Finished(Message):
        def __init__(self, result: BatchResult) -> None:
            super().__init__()
            self.result = result

    def compose(self) -> ComposeResult:
        total = sum(len(item.songs) for item in cast(MonsterSirenApp, self.app).prepared_downloads)
        self.state = ProgressState(total)
        self.frame = 0
        self.downloader: BatchDownloader | None = None
        yield Header(show_clock=False)
        yield Static("正在下载", classes="screen-title")
        yield Static(f"0/{total}", id="download-status")
        with VerticalScroll(id="resource-list"):
            yield Static("正在准备资源……", id="resource-status")
        yield RichLog(id="download-log", wrap=True, markup=False)

    def on_mount(self) -> None:
        self.set_interval(0.2, self._refresh_progress)
        self.app.call_after_refresh(self._start_download)

    def _start_download(self) -> None:
        self.app.run_worker(
            self._run_download(),
            exclusive=True,
            name="download-batch",
            group="download-batch",
        )

    async def _run_download(self) -> None:
        msr_app = cast(MonsterSirenApp, self.app)
        try:
            http = await msr_app.get_http_client()
            site = MonsterSirenClient(http)
            self.downloader = BatchDownloader(
                site,
                http,
                msr_app.config.download_dir,
                cast(FileNamer, msr_app.namer),
                config=msr_app.config,
                mode=msr_app.download_mode,
            )
            result = await self.downloader.execute(
                msr_app.prepared_downloads,
                progress=self._update_progress,
                resource_progress=self._update_resource,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = _fatal_batch_result(
                msr_app.prepared_downloads, msr_app.config.download_dir, exc
            )
        finally:
            if self.downloader:
                msr_app.unresolved_gids = getattr(self.downloader, "unresolved_gids", ())
        self._refresh_progress()
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
        self.state.completed_songs = update.completed
        self.state.logs.append(f"{update.song.name}：{_outcome_text(update.outcome)}")

    def _update_resource(self, update: ResourceProgress) -> None:
        self.state.update(update)

    def _refresh_progress(self) -> None:
        if not self.is_mounted:
            return
        status = self.query_one("#download-status", Static)
        resources = self.query_one("#resource-status", Static)
        width = max(1, min(status.content_size.width, resources.content_size.width))
        lines = self.state.lines(width=width, frame=self.frame)
        self.frame += 1
        status.update(lines[0])
        resources.update(Text("\n").join(lines[1:]))
        log = self.query_one("#download-log", RichLog)
        while self.state.logs:
            log.write(self.state.logs.popleft())


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
        self.set_focus(self.query_one("#back", Button))

    def action_back_to_catalog(self) -> None:
        self.app.pop_screen()

    def action_quit_app(self) -> None:
        self.app.exit()

    def action_focus_back(self) -> None:
        self.set_focus(self.query_one("#back", Button))

    def action_focus_quit(self) -> None:
        self.set_focus(self.query_one("#quit", Button))

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
        cache_path: Path | None = None,
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
        self.download_mode = DownloadMode.ALL
        self.unresolved_gids: tuple[str, ...] = ()
        self._quitting = False
        self.closing = False
        self.cache_path = cache_path if cache_path is not None else default_cache_path()
        self.catalog_updated_at: datetime | None = None
        self._from_cache = False
        self._refresh_failed = False
        self._cache_save_failed = False
        self._pending_catalog: CachedCatalog | None = None
        self._bootstrap_complete = catalog is not None
        self._http_stack = AsyncExitStack()
        self._http_init_task: asyncio.Task[httpx.AsyncClient] | None = None
        self._cache_tasks: set[asyncio.Task[None]] = set()
        self._cleanup_task: asyncio.Task[None] | None = None

    @property
    def catalog_status(self) -> str:
        parts = []
        if self.catalog_updated_at is not None:
            source = "本地曲库" if self._from_cache else "曲库"
            updated = self.catalog_updated_at.astimezone().strftime("%Y-%m-%d %H:%M")
            parts.append(f"{source}更新于 {updated}")
        if self._loading:
            parts.append("正在刷新……")
        elif self._refresh_failed:
            parts.append("刷新失败，继续使用当前曲库；r 重试")
        if self._pending_catalog is not None:
            parts.append("新曲库将在返回列表后应用")
        if self._cache_save_failed:
            parts.append("本次缓存保存失败")
        return "；".join(parts)

    async def _initialize_http_client(self) -> httpx.AsyncClient:
        client = await asyncio.to_thread(_http_client)
        return await self._http_stack.enter_async_context(client)

    async def get_http_client(self) -> httpx.AsyncClient:
        if self.closing:
            raise asyncio.CancelledError
        task = self._http_init_task
        if task is None or (task.done() and task.exception() is not None):
            self._http_init_task = task = asyncio.create_task(self._initialize_http_client())
        return await asyncio.shield(task)

    async def _cleanup_resources(self) -> None:
        self.closing = True
        workers = list(self.workers)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)
        if self._http_init_task is not None:
            await asyncio.gather(self._http_init_task, return_exceptions=True)
        await asyncio.gather(*self._cache_tasks, return_exceptions=True)
        self._cache_tasks.clear()
        await self._http_stack.aclose()

    async def close_resources(self) -> None:
        self.closing = True
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_resources())
        await asyncio.shield(self._cleanup_task)

    async def on_unmount(self) -> None:
        await self.close_resources()

    async def action_force_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        await self.close_resources()
        self.exit()

    async def on_mount(self) -> None:
        if self.catalog is not None:
            await self.push_screen(CatalogScreen())
        else:
            await self.push_screen(LoadingScreen())
            self.call_after_refresh(self._start_bootstrap)

    def _start_bootstrap(self) -> None:
        if self.closing:
            return
        self.run_worker(self._bootstrap_catalog(), group="catalog-cache", name="read-catalog-cache")

    async def _bootstrap_catalog(self) -> None:
        snapshot = await asyncio.to_thread(load_catalog_cache, self.cache_path)
        if self.closing:
            return
        if snapshot is not None:
            self._replace_catalog(snapshot)
            self._from_cache = True
            await self.switch_screen(CatalogScreen())
        self._bootstrap_complete = True
        self.retry_catalog()

    def retry_catalog(self) -> None:
        if self._loading or self.closing or not self._bootstrap_complete:
            return
        if isinstance(self.screen, LoadingScreen):
            self.screen.show_loading_message()
        self._loading = True
        self._update_catalog_status()
        self.run_worker(
            self._load_catalog(),
            exclusive=True,
            name="load-catalog",
            group="catalog-refresh",
        )

    def _update_catalog_status(self) -> None:
        if isinstance(self.screen, CatalogScreen) and self.screen._initialized:
            self.screen.refresh_status()

    def _replace_catalog(self, snapshot: CachedCatalog) -> int:
        catalog = snapshot.catalog
        selected = set(self.selection.selected) if self.selection is not None else set()
        old_catalog = self.catalog
        self.album_cache = {
            cid: detail
            for cid, detail in self.album_cache.items()
            if old_catalog is not None
            and old_catalog.album_by_cid.get(cid) == catalog.album_by_cid.get(cid)
            and old_catalog.songs_by_album.get(cid) == catalog.songs_by_album.get(cid)
            and cid in catalog.album_by_cid
        }
        self.catalog = catalog
        self.selection = SelectionModel(catalog, selected)
        self.namer = FileNamer(catalog)
        self.catalog_updated_at = snapshot.fetched_at
        return len(selected - self.selection.selected)

    def apply_pending_catalog(self) -> bool:
        snapshot = self._pending_catalog
        screen = self.screen
        if (
            snapshot is None
            or not isinstance(screen, CatalogScreen)
            or not screen._initialized
            or screen.operation_pending
            or self.closing
        ):
            return False
        self._pending_catalog = None
        removed = self._replace_catalog(snapshot)
        self._from_cache = False
        screen.refresh_albums(target_cid=screen.current_album_cid)
        if removed:
            self.notify(f"曲库已更新，{removed} 首已选歌曲已从官网移除。", severity="warning")
        return True

    async def _load_catalog(self) -> None:
        try:
            http = await self.get_http_client()
            albums, songs = await MonsterSirenClient(http).fetch_catalog()
            snapshot = CachedCatalog(Catalog(albums, songs), datetime.now(UTC))
            self._refresh_failed = False
            self._cache_save_failed = False
            if self.catalog is None:
                self._replace_catalog(snapshot)
                await self.switch_screen(CatalogScreen())
            elif self.catalog.albums == albums and self.catalog.songs == songs:
                self._pending_catalog = None
                self.catalog_updated_at = snapshot.fetched_at
                self._from_cache = False
            else:
                self._pending_catalog = snapshot
                self.apply_pending_catalog()
            task = asyncio.create_task(
                asyncio.to_thread(save_catalog_cache, self.cache_path, snapshot)
            )
            self._cache_tasks.add(task)
            try:
                await asyncio.shield(task)
            except OSError:
                self._cache_save_failed = True
            finally:
                if task.done():
                    self._cache_tasks.discard(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(self.screen, LoadingScreen):
                self.screen.set_error(exc)
            else:
                self._refresh_failed = True
                self.notify(f"曲库刷新失败：{exc}", severity="warning", timeout=6)
        finally:
            self._loading = False
            if not self.closing:
                self._update_catalog_status()

    async def get_album_detail(self, cid: str) -> AlbumDetail:
        # An in-flight request belongs to this catalog's cache. A catalog replacement
        # installs a new dict so a late cancelled response cannot repopulate it.
        cache = self.album_cache
        cached = cache.get(cid)
        if cached is not None:
            return cached
        http = await self.get_http_client()
        detail = await MonsterSirenClient(http).fetch_album(cid)
        cache[cid] = detail
        return detail

    async def prepare_downloads(
        self,
        song_ids: Iterable[str] | None = None,
    ) -> tuple[AlbumDownload, ...]:
        if self.catalog is None or self.selection is None:
            return ()
        selected = self.selection.selected if song_ids is None else set(song_ids)
        ids = [song.cid for song in self.catalog.songs if song.cid in selected]
        http = await self.get_http_client()
        prepared = await prepare_downloads(
            MonsterSirenClient(http),
            self.catalog,
            ids,
            self.config.download_dir,
            cache=self.album_cache,
            concurrency=self.config.max_concurrent_downloads,
        )
        if prepared.failures:
            raise RuntimeError(
                "；".join(error for result in prepared.failures for error in result.errors)
            )
        return prepared.albums


def _album_label(match: AlbumMatch) -> str:
    indicator = {
        SelectionState.FULL: "[green]●[/green]",
        SelectionState.PARTIAL: "[yellow]◐[/yellow]",
        SelectionState.NONE: "[#808080]○[/#808080]",
    }[match.state]
    text = f"{indicator}  {escape(match.album.name.strip())}"
    if match.matched_songs:
        shown = "、".join(escape(song.name) for song in match.matched_songs[:3])
        extra = len(match.matched_songs) - 3
        text += f"\n    [dim]匹配曲目：{shown}{f' 等 {extra + 3} 首' if extra else ''}[/dim]"
    return text


def _song_label(song: SongSummary, selected: bool) -> str:
    indicator = "[green]●[/green]" if selected else "[#808080]○[/#808080]"
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
