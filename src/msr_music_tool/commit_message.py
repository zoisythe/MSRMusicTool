from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

_SUBJECT = re.compile(
    r"(?:build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(?:\([A-Za-z0-9._/-]+\))?!?: \S.*"
)
_REBASE_PREFIXES = ("fixup! ", "squash! ")


def is_conventional_subject(subject: str) -> bool:
    candidate = subject.strip()
    for prefix in _REBASE_PREFIXES:
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    return _SUBJECT.fullmatch(candidate) is not None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Conventional Commit subject.")
    parser.add_argument("commit_message_file", type=Path)
    args = parser.parse_args(argv)
    try:
        subject = args.commit_message_file.read_text(encoding="utf-8-sig").splitlines()[0]
    except (OSError, IndexError) as exc:
        print(f"无法读取提交信息：{exc}", file=sys.stderr)
        return 1
    if is_conventional_subject(subject):
        return 0
    print(
        "提交信息必须符合 Conventional Commits，例如：feat: add album search",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
