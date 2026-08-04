from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .app import MonsterSirenApp
from .config import ConfigError, default_config_path, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="msr-tool",
        description="在终端中搜索并批量下载塞壬唱片音乐、歌词和封面。",
        epilog=(
            f'配置文件位置：{default_config_path()}\n格式：[download] 下设置 directory = "下载目录"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    MonsterSirenApp(config).run()
    return 0
