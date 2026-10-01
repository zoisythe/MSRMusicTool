from __future__ import annotations

import asyncio
import io
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from e2e_server import FixtureServer
from textual.widgets import ProgressBar, Select

from msr_music_tool.app import ConfirmScreen, DownloadScreen, MonsterSirenApp, ResultScreen
from msr_music_tool.catalog import Catalog
from msr_music_tool.config import AppConfig, RpcConfig
from msr_music_tool.downloader import BatchDownloader, SongOutcome
from msr_music_tool.models import DownloadMode
from msr_music_tool.naming import FileNamer
from msr_music_tool.progress import CliProgress, ProgressState
from msr_music_tool.service import prepare_downloads
from msr_music_tool.site import MonsterSirenClient
from msr_music_tool.transfers import ResourceProgress


@pytest.fixture
def server():
    with FixtureServer() as fixture:
        yield fixture


def config_file(
    path: Path,
    output: Path,
    *,
    backend: str = "http",
    endpoint: str = "",
    secret: str = "acceptance-secret",
    style: str = "=>",
    extra: str = "",
) -> Path:
    import json

    path.write_text(
        f'[download]\ndirectory = {json.dumps(str(output))}\nbackend = "{backend}"\n'
        f'max_concurrent_downloads = 2\nprogress_style = "{style}"\n[rpc]\n'
        f'endpoint = "{endpoint or "http://127.0.0.1:6800/jsonrpc"}"\n'
        f'secret = "{secret}"\npoll_interval = 0.1\n{extra}',
        encoding="utf-8",
    )
    return path


def cli(
    server: FixtureServer,
    *args: str,
    interrupt_after: float | None = None,
) -> subprocess.CompletedProcess:
    # Exercise the actual entrypoint; inject only the fixture's site URL in the child harness.
    code = (
        "import sys; import msr_music_tool.site as site; site.BASE_URL=sys.argv.pop(1); "
        "from msr_music_tool.cli import main; raise SystemExit(main(sys.argv[1:]))"
    )
    if interrupt_after is not None:
        code = (
            "import threading,signal; "
            f"timer=threading.Timer({interrupt_after},lambda:signal.raise_signal(signal.SIGINT)); "
            "timer.daemon=True; timer.start(); " + code
        )
    return subprocess.run(
        [sys.executable, "-c", code, server.url, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
    )


def test_cli_search_and_invalid_input(server, tmp_path):
    config = config_file(tmp_path / "config.toml", tmp_path / "out")
    result = cli(
        server,
        "search",
        "fIRst",
        "111111",
        "https://monster-siren.hypergryph.com/m/music/222222?from=test",
        "https://monster-siren.hypergryph.com/music/111111",
        "--config",
        str(config),
    )
    assert result.returncode == 0, result.stderr
    ids = [line.split("\t")[0] for line in result.stdout.splitlines()[1:]]
    assert ids == ["111111", "333333", "222222"]
    assert "Album a1" in result.stdout and "MSR" in result.stdout
    assert cli(server, "search", "absent", "--config", str(config)).returncode == 1
    for bad in ("First", "https://monster-siren.hypergryph.com/music/111111", "１１１１１１"):
        before = server.calls.copy()
        result = cli(server, "download", bad, "--config", str(config))
        assert result.returncode == 2 and server.calls == before
    assert cli(server, "search", "https://evil.test/music/111111").returncode == 2
    assert (
        cli(server, "download", "111111", "--config", str(tmp_path / "absent.toml")).returncode == 2
    )
    assert cli(server, "download", "111111", "--concurrency", "10").returncode == 2


@pytest.mark.parametrize("mode,count", [("all", 6), ("audio", 2), ("audio-lyrics", 4)])
@pytest.mark.parametrize("style", ["=>", "#", "ILoveCandy"])
def test_cli_modes_progress_and_skip(server, tmp_path, mode, count, style):
    out = tmp_path / "override"
    config = config_file(tmp_path / "config.toml", tmp_path / "unused", style=style)
    result = cli(
        server,
        "--config",
        str(config),
        "download",
        "111111",
        "222222",
        "111111",
        "--mode",
        mode,
        "--output",
        str(out),
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout and "\x1b" not in result.stderr
    assert len(list(out.iterdir())) == count
    assert not (tmp_path / "unused").exists()
    assert "歌曲 0/2" in result.stderr and "/s" in result.stderr
    assert server.max_active >= 2 and server.max_active <= 2
    if mode == "all":
        assert server.calls[("GET", "/a1.jpg")] == 1
    else:
        assert server.calls[("GET", "/a1.jpg")] == 0
    if mode != "audio":
        assert (out / "塞壬唱片-MSR - Second.lrc").read_text("utf-8") == "fallback intro"
    audio = next(out.glob("*111111*.wav"))
    audio.write_bytes(b"partial")
    repeated = cli(
        server,
        "download",
        "111111",
        "222222",
        "--mode",
        mode,
        "--config",
        str(config),
        "--output",
        str(out),
    )
    assert repeated.returncode == 0, repeated.stderr
    assert audio.stat().st_size == server.media_size
    assert "skipped" in repeated.stderr
    assert not list(out.glob("*.part"))


def test_cli_unknown_length_retry_and_partial_failure(server, tmp_path):
    config = config_file(tmp_path / "config.toml", tmp_path / "out")
    result = cli(
        server,
        "download",
        "444444",
        "555555",
        "666666",
        "999999",
        "--mode",
        "audio",
        "--config",
        str(config),
    )
    assert result.returncode == 1, result.stderr
    assert "重试" in result.stderr and server.calls[("GET", "/555555.wav")] == 2
    assert "999999" in result.stderr and "不存在" in result.stderr
    assert len(list((tmp_path / "out").glob("*.wav"))) == 2
    assert f"{2 * server.media_size / 1024:.1f} KiB 0.0 B/s · 歌曲 4/4" in result.stderr
    assert not list((tmp_path / "out").glob("*.part"))


def test_cli_rejects_invalid_configuration_before_network(server, tmp_path):
    path = tmp_path / "config.toml"
    invalid = [
        "max_concurrent_downloads = true",
        "max_concurrent_downloads = 0",
        "max_concurrent_downloads = 33",
        'backend = "unsupported"',
        'progress_style = "invalid"',
        "[rpc]\npoll_interval = nan",
        "[rpc]\npoll_interval = 0",
        "[rpc]\nsecret = 123",
        '[rpc]\nendpoint = "ftp://localhost/"',
        '[rpc]\ndirectory = "relative"',
    ]
    before = server.calls.copy()
    for value in invalid:
        path.write_text('[download]\ndirectory = "out"\n' + value, encoding="utf-8")
        result = cli(server, "download", "111111", "--config", str(path))
        assert result.returncode == 2, result.stderr
        assert server.calls == before


@pytest.mark.asyncio
async def test_http_events_before_song_completion_and_cancellation(server, tmp_path, monkeypatch):
    monkeypatch.setattr("msr_music_tool.site.BASE_URL", server.url)
    async with httpx.AsyncClient(trust_env=False) as http:
        site = MonsterSirenClient(http)
        catalog = Catalog(*(await site.fetch_catalog()))
        config = AppConfig(
            tmp_path / "config.toml", tmp_path / "out", False, max_concurrent_downloads=2
        )
        prepared = await prepare_downloads(site, catalog, ["111111", "222222"], config.download_dir)
        events = []
        finished = []
        downloader = BatchDownloader(
            site,
            http,
            config.download_dir,
            FileNamer(catalog),
            mode=DownloadMode.AUDIO,
            config=config,
        )
        await downloader.execute(
            prepared.albums,
            resource_progress=lambda event: events.append((event, len(finished))),
            progress=finished.append,
        )
        moving = [
            event
            for event, count in events
            if count == 0 and event.status == "active" and event.downloaded > 0 and event.speed > 0
        ]
        assert len(moving) > 2
        assert len({event.resource_id for event in moving}) == 2
        long = await prepare_downloads(site, catalog, ["777777"], config.download_dir)
        downloading = asyncio.Event()

        def observe(event):
            if event.downloaded > 0:
                downloading.set()

        downloader = BatchDownloader(
            site,
            http,
            config.download_dir,
            FileNamer(catalog),
            mode=DownloadMode.AUDIO,
            config=config,
        )
        task = asyncio.create_task(downloader.execute(long.albums, resource_progress=observe))
        await asyncio.wait_for(downloading.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not list(config.download_dir.glob("*.part"))


@pytest.mark.asyncio
async def test_cancel_during_threaded_file_open_closes_handle(server, tmp_path, monkeypatch):
    monkeypatch.setattr("msr_music_tool.site.BASE_URL", server.url)
    opened = threading.Event()
    original_open = Path.open
    handles = []

    def slow_open(path, mode="r", *args, **kwargs):
        file = original_open(path, mode, *args, **kwargs)
        if path.suffix == ".part" and mode == "wb":
            handles.append(file)
            opened.set()
            time.sleep(0.2)
        return file

    async with httpx.AsyncClient(trust_env=False) as http:
        site = MonsterSirenClient(http)
        catalog = Catalog(*(await site.fetch_catalog()))
        output = tmp_path / "out"
        prepared = await prepare_downloads(site, catalog, ["777777"], output)
        monkeypatch.setattr(Path, "open", slow_open)
        downloader = BatchDownloader(
            site, http, output, FileNamer(catalog), mode=DownloadMode.AUDIO
        )
        task = asyncio.create_task(downloader.execute(prepared.albums))
        assert await asyncio.to_thread(opened.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert handles and all(file.closed for file in handles)
        assert not list(output.glob("*.part"))


@pytest.mark.asyncio
async def test_tui_modes_live_progress_resize_and_interrupt(server, tmp_path, monkeypatch):
    monkeypatch.setattr("msr_music_tool.site.BASE_URL", server.url)
    # Keep both transfers running while Textual's click driver waits for idle;
    # a short fixture can finish before observation on a busy machine.
    server.media_size = 8 * 1024 * 1024
    config = AppConfig(
        tmp_path / "config.toml", tmp_path / "out", False, max_concurrent_downloads=2
    )
    app = MonsterSirenApp(config)
    async with app.run_test(size=(100, 32)) as pilot:
        for _ in range(100):
            await asyncio.sleep(0.03)
            if app.selection is not None:
                break
        app.selection.selected.update(["111111", "777777"])
        app.prepared_downloads = await app.prepare_downloads()
        await app.push_screen(ConfirmScreen())
        app.screen.query_one(Select).value = "audio"
        await pilot.click("#confirm")
        for _ in range(100):
            await asyncio.sleep(0.03)
            if isinstance(app.screen, DownloadScreen) and any(
                e.downloaded > 0 for e in app.screen.state.resources.values()
            ):
                break
        assert app.download_mode is DownloadMode.AUDIO
        screen = app.screen
        assert isinstance(screen, DownloadScreen)
        assert screen.state.completed_songs == 0
        await pilot.resize_terminal(50, 18)
        await asyncio.sleep(0.25)
        assert screen.query_one(ProgressBar).progress > 0
        assert any(event.speed > 0 for event in screen.state.resources.values())
        await pilot.press("ctrl+c")
        assert not app.is_running
    assert not list(config.download_dir.glob("*.part"))


@pytest.mark.parametrize("style", ["=>", "#", "ILoveCandy"])
def test_terminal_renderer_uses_requested_style_and_cell_width(style, monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    terminal = Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    monkeypatch.setattr("shutil.get_terminal_size", lambda *_args: os.terminal_size((36, 12)))
    state = ProgressState(2)
    state.update(
        ResourceProgress("audio", ("111111",), "中文歌曲名称 · 音频", "active", 50, 100, 20)
    )
    renderer = CliProgress(state, style)
    renderer.render()
    state.update(
        ResourceProgress("audio", ("111111",), "中文歌曲名称 · 音频", "complete", 100, 100)
    )
    renderer.render()
    output = terminal.getvalue()
    assert "\x1b[" in output and "50%" in output
    assert {"=>": "=", "#": "#", "ILoveCandy": "c"}[style] in output


def test_rpc_cli_recovery_auth_mapping_and_partial_failure(server, tmp_path):
    config = config_file(
        tmp_path / "config.toml",
        tmp_path / "out",
        backend="aria2",
        endpoint=server.url + "/jsonrpc",
    )
    server.lose_add = True
    server.fail_polls = 2
    result = cli(server, "download", "111111", "222222", "--config", str(config))
    assert result.returncode == 0, result.stderr
    assert len(list((tmp_path / "out").iterdir())) == 6
    assert server.calls[("RPC", "aria2.addUri")] == 4  # two audios, one lyric, one shared cover
    assert server.calls[("GET", "/a1.jpg")] == 1
    assert not list((tmp_path / "out").glob("*.part"))
    bad = config_file(
        tmp_path / "bad.toml",
        tmp_path / "bad",
        backend="aria2",
        endpoint=server.url + "/jsonrpc",
        secret="wrong",
    )
    result = cli(server, "download", "111111", "--config", str(bad))
    assert result.returncode == 1 and "wrong" not in result.stderr
    mapped = config_file(
        tmp_path / "mapped.toml",
        tmp_path / "mapped",
        backend="aria2",
        endpoint=server.url + "/jsonrpc",
        extra=f'directory = "{(tmp_path / "mapped").as_posix()}"\n',
    )
    assert (
        cli(server, "download", "222222", "--config", str(mapped), "--mode", "audio").returncode
        == 0
    )
    assert (
        cli(
            server,
            "download",
            "222222",
            "--config",
            str(mapped),
            "--output",
            str(tmp_path / "elsewhere"),
        ).returncode
        == 2
    )
    result = cli(server, "download", "666666", "222222", "--config", str(config), "--mode", "audio")
    assert result.returncode == 1 and "222222 Second：skipped" in result.stderr


@pytest.mark.parametrize("backend", ["http", "aria2"])
def test_cli_sigint_cleans_only_owned_tasks(server, tmp_path, backend):
    config = config_file(
        tmp_path / "config.toml",
        tmp_path / "out",
        backend=backend,
        endpoint=server.url + "/jsonrpc",
    )
    result = cli(
        server,
        "download",
        "777777",
        "--mode",
        "audio",
        "--config",
        str(config),
        interrupt_after=1.5,
    )
    assert result.returncode == 130, result.stderr
    assert "已中断" in result.stderr
    assert not list((tmp_path / "out").glob("*.part*"))
    if backend == "aria2":
        assert all(job["status"] == "removed" for job in server.jobs.values())


def test_rpc_persistent_disconnect_retains_uncertain_files_and_stops_submission(server, tmp_path):
    config = config_file(
        tmp_path / "config.toml",
        tmp_path / "out",
        backend="aria2",
        endpoint=server.url + "/jsonrpc",
    )
    server.fail_polls = 100
    result = cli(
        server, "download", "111111", "222222", "333333", "--mode", "audio", "--config", str(config)
    )
    assert result.returncode == 1
    assert "取消无法确认" in result.stderr and "GID" in result.stderr
    assert "acceptance-secret" not in result.stderr
    assert server.calls[("RPC", "aria2.addUri")] <= 2
    assert list((tmp_path / "out").glob("*.part"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,count", [("all", 6), ("audio", 2), ("audio-lyrics", 4)])
async def test_tui_real_download_result_and_mode_selection(
    server, tmp_path, monkeypatch, mode, count
):
    monkeypatch.setattr("msr_music_tool.site.BASE_URL", server.url)
    config = AppConfig(tmp_path / "config.toml", tmp_path / "out", False)
    app = MonsterSirenApp(config)
    async with app.run_test(size=(100, 32)) as pilot:
        for _ in range(100):
            await asyncio.sleep(0.03)
            if app.selection is not None:
                break
        app.selection.selected.update(["111111", "222222"])
        app.prepared_downloads = await app.prepare_downloads()
        await app.push_screen(ConfirmScreen())
        selector = app.screen.query_one(Select)
        selector.focus()
        await pilot.press("enter")
        for _ in range(["all", "audio", "audio-lyrics"].index(mode)):
            await pilot.press("down")
        await pilot.press("enter")
        assert selector.value == mode
        await pilot.click("#confirm")
        for _ in range(150):
            await asyncio.sleep(0.03)
            if isinstance(app.screen, ResultScreen):
                break
        assert isinstance(app.screen, ResultScreen)
        assert app.last_result.count(SongOutcome.SUCCESS) == 2
        assert not app.selection.selected
        await pilot.press("escape")
    assert len(list(config.download_dir.iterdir())) == count


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(params=["MSR_ARIA2_BINARY", "MSR_ARIA_NEXT_BINARY"])
def real_engine(request, tmp_path):
    binary = os.environ.get(request.param)
    if not binary:
        pytest.skip(f"set {request.param} to run real-engine acceptance")
    port = free_port()
    endpoint = f"http://127.0.0.1:{port}/jsonrpc"
    log = (tmp_path / "engine.log").open("wb")
    process = subprocess.Popen(
        [
            binary,
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={port}",
            "--rpc-secret=acceptance-secret",
            "--max-concurrent-downloads=2",
            f"--dir={tmp_path}",
            "--console-log-level=warn",
        ],
        stdout=log,
        stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        for _ in range(80):
            if process.poll() is not None:
                pytest.fail((tmp_path / "engine.log").read_text(errors="replace"))
            try:
                version = httpx.post(
                    endpoint,
                    json={
                        "jsonrpc": "2.0",
                        "id": "check",
                        "method": "aria2.getVersion",
                        "params": ["token:acceptance-secret"],
                    },
                    timeout=1,
                ).json()
                if "result" in version:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("engine did not start")
        print(f"Real engine: {request.param} {version['result']['version']}")
        yield endpoint
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()


def test_real_engine_cli_full_download_and_ambiguous_submission(server, tmp_path, real_engine):
    server.rpc_target = real_engine
    server.lose_add = True
    server.fail_polls = 1
    config = config_file(
        tmp_path / "config.toml",
        tmp_path / "out",
        backend="aria2",
        endpoint=server.url + "/jsonrpc",
    )
    result = cli(server, "download", "111111", "222222", "--config", str(config))
    assert result.returncode == 0, result.stderr
    files = list((tmp_path / "out").iterdir())
    assert len(files) == 6
    assert server.calls[("RPC", "aria2.addUri")] == 4
    assert server.calls[("GET", "/a1.jpg")] == 1
    assert not list((tmp_path / "out").glob("*.part"))
    assert "歌曲 0/2" in result.stderr and "/s" in result.stderr


@pytest.mark.asyncio
async def test_real_engine_progress_cancel_and_isolation(
    server, tmp_path, real_engine, monkeypatch
):
    monkeypatch.setattr("msr_music_tool.site.BASE_URL", server.url)
    async with httpx.AsyncClient(trust_env=False) as http:
        # An unrelated paused task must survive cancellation of our batch.
        reply = await http.post(
            real_engine,
            json={
                "jsonrpc": "2.0",
                "id": "outside",
                "method": "aria2.addUri",
                "params": [
                    "token:acceptance-secret",
                    [server.url + "/777777.wav"],
                    {"pause": "true", "dir": str(tmp_path), "out": "outside.wav"},
                ],
            },
        )
        outside = reply.json()["result"]
        site = MonsterSirenClient(http)
        catalog = Catalog(*(await site.fetch_catalog()))
        config = AppConfig(
            tmp_path / "config.toml",
            tmp_path / "out",
            False,
            backend="aria2",
            rpc=RpcConfig(real_engine, "acceptance-secret", 0.1),
        )
        prepared = await prepare_downloads(site, catalog, ["777777"], config.download_dir)
        downloader = BatchDownloader(
            site,
            http,
            config.download_dir,
            FileNamer(catalog),
            config=config,
            mode=DownloadMode.AUDIO,
        )
        moving = asyncio.Event()
        events = []

        def observe(event):
            events.append(event)
            if event.status == "active" and event.downloaded > 0 and event.speed > 0:
                moving.set()

        task = asyncio.create_task(downloader.execute(prepared.albums, resource_progress=observe))
        await asyncio.wait_for(moving.wait(), 8)
        await asyncio.sleep(0.25)
        assert len([event for event in events if event.downloaded > 0]) > 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not downloader.unresolved_gids
        assert not list(config.download_dir.glob("*.part"))
        reply = await http.post(
            real_engine,
            json={
                "jsonrpc": "2.0",
                "id": "check",
                "method": "aria2.tellStatus",
                "params": ["token:acceptance-secret", outside, ["status"]],
            },
        )
        assert reply.json()["result"]["status"] == "paused"
