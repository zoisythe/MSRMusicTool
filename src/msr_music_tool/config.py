from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_path


class ConfigError(ValueError):
    """Raised when an existing configuration file is invalid."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    config_path: Path
    download_dir: Path
    is_configured: bool


def default_config_path() -> Path:
    return user_config_path("MSRMusicTool", appauthor=False) / "config.toml"


def load_config(*, cwd: Path | None = None, config_path: Path | None = None) -> AppConfig:
    working_dir = (cwd or Path.cwd()).resolve()
    path = (config_path or default_config_path()).expanduser().resolve()
    if not path.exists():
        return AppConfig(
            config_path=path,
            download_dir=working_dir / "MonsterSirenRecording",
            is_configured=False,
        )

    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc

    download = raw.get("download")
    if not isinstance(download, dict):
        raise ConfigError(f"配置文件 {path} 缺少 [download] 表")
    directory = download.get("directory")
    if not isinstance(directory, str) or not directory.strip():
        raise ConfigError(f"配置文件 {path} 的 download.directory 必须是非空字符串")

    configured_dir = Path(directory.strip()).expanduser()
    if not configured_dir.is_absolute():
        configured_dir = path.parent / configured_dir
    return AppConfig(
        config_path=path,
        download_dir=configured_dir.resolve(),
        is_configured=True,
    )
