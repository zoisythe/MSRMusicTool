from __future__ import annotations

import os
import subprocess
import sys

from msr_music_tool.app import MonsterSirenApp
from msr_music_tool.cli import build_parser


def test_only_cli_name_is_msr_tool() -> None:
    assert build_parser().prog == "msr-tool"
    assert MonsterSirenApp.TITLE == "MSRMusicTool"


def test_help_uses_utf8_when_inherited_stream_encoding_is_legacy() -> None:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp1252"

    result = subprocess.run(
        [sys.executable, "-m", "msr_music_tool", "--help"],
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    help_text = result.stdout.decode("utf-8")
    assert "usage: msr-tool" in help_text
    assert "配置文件位置" in help_text
