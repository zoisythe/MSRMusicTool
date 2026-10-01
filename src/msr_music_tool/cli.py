from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx

from . import __version__
from .catalog import Catalog
from .config import AppConfig, ConfigError, default_config_path, load_config
from .downloader import BatchDownloader, DownloadProgress, SongOutcome
from .models import DownloadMode
from .naming import FileNamer
from .progress import CliProgress, ProgressState
from .service import SONG_ID, http_client, prepare_downloads, query_identifier, search
from .site import BASE_URL, MonsterSirenClient, SiteError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="msr-tool",
        description="搜索并批量下载塞壬唱片音乐、歌词和封面；无子命令启动 TUI。",
        epilog=(
            f"配置文件位置：{default_config_path()}\n并发、进度风格和 RPC 参数只在 TOML 中配置。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, help="TOML 配置文件")
    parser.add_argument("--output", type=Path, help="临时覆盖下载目录")
    subparsers = parser.add_subparsers(dest="command")
    for command, help_text in (
        ("search", "搜索名称、六位 ID 或官网链接"),
        ("download", "按六位歌曲 ID 下载"),
    ):
        sub = subparsers.add_parser(command, help=help_text)
        sub.add_argument(
            "items", nargs="+", help="多个名称/链接" if command == "search" else "歌曲 ID"
        )
        sub.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        sub.add_argument("--output", type=Path, default=argparse.SUPPRESS)
        if command == "download":
            sub.add_argument("--mode", choices=[mode.value for mode in DownloadMode], default="all")
    return parser


def _configure_standard_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


async def run_cli(args: argparse.Namespace, config: AppConfig) -> int:
    async with http_client() as http:
        site = MonsterSirenClient(http)
        albums, songs = await site.fetch_catalog()
        catalog = Catalog(albums, songs)
        if args.command == "search":
            matches, missing = search(catalog, args.items)
            if matches:
                print("ID\t歌曲\t专辑\t艺人\t链接")
            for song in matches:
                album = catalog.album_by_cid.get(song.album_cid)
                fields = [
                    song.cid,
                    song.name,
                    album.name if album else song.album_cid,
                    " / ".join(song.artists),
                    f"{BASE_URL}/music/{song.cid}",
                ]
                print("\t".join(field.replace("\t", " ").replace("\n", " ") for field in fields))
            for query in missing:
                print(f"无匹配：{query}", file=sys.stderr)
            return 1 if missing else 0
        ids = list(dict.fromkeys(args.items))
        prepared = await prepare_downloads(
            site, catalog, ids, config.download_dir, concurrency=config.max_concurrent_downloads
        )
        state = ProgressState(len(ids))
        state.completed_songs = len(prepared.failures)

        def completed(event: DownloadProgress) -> None:
            state.completed_songs = len(prepared.failures) + event.completed

        downloader = BatchDownloader(
            site,
            http,
            config.download_dir,
            FileNamer(catalog),
            config=config,
            mode=DownloadMode(args.mode),
        )
        results = list(prepared.failures)
        try:
            if prepared.albums:
                async with CliProgress(state, config.progress_style):
                    result = await downloader.execute(
                        prepared.albums, progress=completed, resource_progress=state.update
                    )
                results.extend(result.songs)
        finally:
            for gid in downloader.unresolved_gids:
                print(f"RPC 取消无法确认，保留临时文件，GID：{gid}", file=sys.stderr)
        for result in results:
            print(f"{result.song.cid} {result.song.name}：{result.outcome.value}", file=sys.stderr)
            for error in result.errors:
                print(f"  {error}", file=sys.stderr)
        return 1 if any(result.outcome is SongOutcome.FAILED for result in results) else 0


def main(argv: Sequence[str] | None = None) -> int:
    _configure_standard_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "download":
        for item in args.items:
            if not SONG_ID.fullmatch(item):
                parser.error(f"download 仅接受六位 ASCII 数字歌曲 ID：{item}")
    elif args.command == "search":
        for item in args.items:
            if not item.strip():
                parser.error("搜索内容不能为空")
            try:
                query_identifier(item)
            except ValueError as exc:
                parser.error(str(exc))
    try:
        config = load_config(
            config_path=args.config, require_exists=args.config is not None, output=args.output
        )
        if args.command is None:
            from .app import MonsterSirenApp

            app = MonsterSirenApp(config)
            app.run()
            for gid in app.unresolved_gids:
                print(f"RPC 取消无法确认，保留临时文件，GID：{gid}", file=sys.stderr)
            return 130 if app._quitting else 0
        return asyncio.run(run_cli(args, config))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except (SiteError, OSError, httpx.HTTPError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
