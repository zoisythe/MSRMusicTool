from __future__ import annotations

from msr_music_tool.app import MonsterSirenApp
from msr_music_tool.cli import build_parser


def test_only_cli_name_is_msr_tool() -> None:
    assert build_parser().prog == "msr-tool"
    assert MonsterSirenApp.TITLE == "MSRMusicTool"
