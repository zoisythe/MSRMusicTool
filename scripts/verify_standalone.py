from __future__ import annotations

import platform
import subprocess

from build_standalone import ROOT, standalone_name


def main() -> int:
    suffix = ".exe" if platform.system() == "Windows" else ""
    artifact = (ROOT / "release" / f"{standalone_name()}{suffix}").resolve()
    completed = subprocess.run(
        [str(artifact), "--help"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0 or b"usage: msr-tool" not in completed.stdout:
        raise RuntimeError(
            f"Standalone smoke test failed ({completed.returncode}):\n"
            f"stdout:\n{completed.stdout.decode(errors='replace')}\n"
            f"stderr:\n{completed.stderr.decode(errors='replace')}"
        )
    print(f"Verified {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
