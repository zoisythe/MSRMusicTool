from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from platformdirs import user_config_path


class ConfigError(ValueError):
    """Raised when an existing configuration file is invalid."""


@dataclass(frozen=True, slots=True)
class RpcConfig:
    endpoint: str = "http://127.0.0.1:6800/jsonrpc"
    secret: str = field(default="", repr=False)
    poll_interval: float = 0.5
    directory: str | None = None


@dataclass(frozen=True, slots=True)
class AppConfig:
    config_path: Path
    download_dir: Path
    is_configured: bool
    backend: str = "http"
    max_concurrent_downloads: int = 4
    progress_style: str = "=>"
    rpc: RpcConfig = field(default_factory=RpcConfig)


def default_config_path() -> Path:
    return user_config_path("MSRMusicTool", appauthor=False) / "config.toml"


def load_config(
    *,
    cwd: Path | None = None,
    config_path: Path | None = None,
    require_exists: bool = False,
    output: Path | None = None,
) -> AppConfig:
    working_dir = (cwd or Path.cwd()).resolve()
    path = (config_path or default_config_path()).expanduser().resolve()
    if not path.exists():
        if require_exists:
            raise ConfigError(f"配置文件不存在：{path}")
        config = AppConfig(
            config_path=path,
            download_dir=working_dir / "MonsterSirenRecording",
            is_configured=False,
        )
        return _override_output(config, output, working_dir)

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
    backend = download.get("backend", "http")
    if backend not in ("http", "aria2"):
        raise ConfigError(f"{path}: download.backend 必须为 http 或 aria2")
    concurrency = download.get("max_concurrent_downloads", 4)
    if type(concurrency) is not int or not 1 <= concurrency <= 32:
        raise ConfigError(f"{path}: download.max_concurrent_downloads 必须为 1–32 的整数")
    style = download.get("progress_style", "=>")
    if style not in ("=>", "#", "ILoveCandy"):
        raise ConfigError(f"{path}: download.progress_style 必须为 =>、# 或 ILoveCandy")
    rpc = raw.get("rpc", {})
    if not isinstance(rpc, dict):
        raise ConfigError(f"{path}: rpc 必须是表")
    endpoint = rpc.get("endpoint", RpcConfig().endpoint)
    try:
        parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
        valid_endpoint = (
            parsed is not None
            and parsed.scheme in ("http", "https")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and not parsed.query
            and parsed.port != 0
        )
    except ValueError:
        valid_endpoint = False
    if not valid_endpoint:
        raise ConfigError(f"{path}: rpc.endpoint 必须为不含凭据的 HTTP(S) URL")
    secret = rpc.get("secret", "")
    if not isinstance(secret, str):
        raise ConfigError(f"{path}: rpc.secret 必须是字符串")
    interval = rpc.get("poll_interval", 0.5)
    if (
        type(interval) not in (int, float)
        or not math.isfinite(interval)
        or not 0.1 <= interval <= 60
    ):
        raise ConfigError(f"{path}: rpc.poll_interval 必须为 0.1–60 秒")
    remote_dir = rpc.get("directory")
    if remote_dir is not None:
        # Accept absolute Windows paths even when the client runs on POSIX, and vice versa.
        from pathlib import PurePosixPath, PureWindowsPath

        if not isinstance(remote_dir, str) or not (
            PurePosixPath(remote_dir).is_absolute() or PureWindowsPath(remote_dir).is_absolute()
        ):
            raise ConfigError(f"{path}: rpc.directory 必须为引擎侧绝对路径")
    config = AppConfig(
        config_path=path,
        download_dir=configured_dir.resolve(),
        is_configured=True,
        backend=backend,
        max_concurrent_downloads=concurrency,
        progress_style=style,
        rpc=RpcConfig(endpoint, secret, float(interval), remote_dir),
    )
    return _override_output(config, output, working_dir)


def _override_output(config: AppConfig, output: Path | None, cwd: Path) -> AppConfig:
    if output is None:
        return config
    directory = output.expanduser()
    directory = (cwd / directory).resolve() if not directory.is_absolute() else directory.resolve()
    if config.backend == "aria2" and config.rpc.directory and directory != config.download_dir:
        raise ConfigError("使用 rpc.directory 映射时，--output 必须与 download.directory 一致")
    return replace(config, download_dir=directory)
