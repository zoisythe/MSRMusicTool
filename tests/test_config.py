from __future__ import annotations

from pathlib import Path

import pytest

from msr_music_tool.config import ConfigError, default_config_path, load_config


def test_default_config_path_uses_project_name() -> None:
    assert default_config_path().parent.name == "MSRMusicTool"


def test_missing_config_uses_working_directory(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, config_path=tmp_path / "missing.toml")

    assert config.download_dir == tmp_path / "MonsterSirenRecording"
    assert not config.is_configured
    assert not config.download_dir.exists()


def test_configured_relative_directory_is_relative_to_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "settings"
    config_dir.mkdir()
    path = config_dir / "config.toml"
    path.write_text('[download]\ndirectory = "../music"\n', encoding="utf-8")

    config = load_config(cwd=tmp_path / "elsewhere", config_path=path)

    assert config.download_dir == tmp_path / "music"
    assert config.is_configured
    assert not config.download_dir.exists()


@pytest.mark.parametrize(
    "content, message",
    [
        ("not toml =", "无法读取配置文件"),
        ("name = 'x'", "缺少"),
        ("[download]\ndirectory = ''", "必须是非空字符串"),
        ("[download]\ndirectory = 1", "必须是非空字符串"),
    ],
)
def test_invalid_config_is_not_silently_ignored(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(config_path=path)
