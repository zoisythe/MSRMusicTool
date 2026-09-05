from __future__ import annotations

import platform
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parents[1]


def standalone_name() -> str:
    system = {"darwin": "macos"}.get(platform.system().casefold(), platform.system().casefold())
    machine = platform.machine().casefold()
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine.replace(" ", "_"))
    return f"msr-tool-{system}-{architecture}"


def main() -> int:
    release_dir = ROOT / "release"
    work_dir = ROOT / "build" / "pyinstaller"
    spec_dir = ROOT / "build" / "pyinstaller-spec"
    for directory in (release_dir, work_dir, spec_dir):
        directory.mkdir(parents=True, exist_ok=True)

    name = standalone_name()
    PyInstaller.__main__.run(
        [
            "--noconfirm",
            "--clean",
            "--noupx",
            "--onefile",
            "--console",
            "--log-level",
            "WARN",
            "--name",
            name,
            "--paths",
            str(ROOT / "src"),
            "--collect-data",
            "msr_music_tool",
            "--hidden-import",
            "socksio",
            "--hidden-import",
            "httpcore._async.socks_proxy",
            "--hidden-import",
            "httpcore._sync.socks_proxy",
            "--distpath",
            str(release_dir),
            "--workpath",
            str(work_dir),
            "--specpath",
            str(spec_dir),
            str(ROOT / "scripts" / "standalone_entry.py"),
        ]
    )
    suffix = ".exe" if platform.system() == "Windows" else ""
    artifact = release_dir / f"{name}{suffix}"
    if not artifact.is_file():
        raise RuntimeError(f"PyInstaller did not create {artifact}")
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
