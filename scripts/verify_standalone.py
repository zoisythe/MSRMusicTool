from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import tempfile
from pathlib import Path

import httpx
from build_standalone import ROOT, standalone_name


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify release CLI and optionally real downloads")
    parser.add_argument(
        "--live",
        action="store_true",
        help="search/download official media in a temporary directory",
    )
    parser.add_argument("--wheel", type=Path, help="verify an isolated wheel installation using uv")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    suffix = ".exe" if platform.system() == "Windows" else ""
    artifact = (args.wheel or ROOT / "release" / f"{standalone_name()}{suffix}").resolve()
    command = (
        ["uv", "run", "--no-project", "--with", str(artifact), "msr-tool"]
        if args.wheel
        else [str(artifact)]
    )
    checks = []

    def run(arguments: list[str], expected: int = 0) -> subprocess.CompletedProcess:
        result = subprocess.run(
            command + arguments, capture_output=True, text=True, encoding="utf-8", timeout=600
        )
        checks.append(
            {
                "arguments": arguments,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if result.returncode != expected:
            raise RuntimeError(f"Release verification failed: {arguments}\n{result.stderr}")
        return result

    assert "usage: msr-tool" in run(["--help"]).stdout
    version = run(["--version"]).stdout.strip()
    run(["download", "not-an-id"], 2)
    run(["download", "111111", "--mode", "invalid"], 2)
    files = []
    if args.live:
        with tempfile.TemporaryDirectory(prefix="msr-release-acceptance-") as temporary:
            output = Path(temporary) / "music"
            config = Path(temporary) / "config.toml"
            config.write_text(
                "[download]\ndirectory = " + json.dumps(str(output)) + "\n", encoding="utf-8"
            )
            songs = httpx.get("https://monster-siren.hypergryph.com/api/songs", timeout=60).json()[
                "data"
            ]["list"]
            cid = songs[0]["cid"]
            search = run(
                [
                    "search",
                    cid,
                    "https://monster-siren.hypergryph.com/m/music/" + cid,
                    "--config",
                    str(config),
                ]
            )
            assert len(search.stdout.splitlines()) == 2
            result = run(["download", cid, "--config", str(config)])
            assert "/s" in result.stderr and "歌曲 0/1" in result.stderr
            for path in sorted(output.iterdir()):
                assert path.suffix != ".part"
                files.append(
                    {
                        "name": path.name,
                        "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
            assert len(files) == 3
            repeated = run(["download", cid, "--mode", "audio", "--config", str(config)])
            assert "skipped" in repeated.stderr
    report = {
        "platform": platform.platform(),
        "artifact": str(artifact),
        "version": version,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "live": args.live,
        "files": files,
        "checks": checks,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Verified {version}: {artifact}; live={args.live}, checks={len(checks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
