from __future__ import annotations

import pytest

from msr_music_tool.commit_message import is_conventional_subject


@pytest.mark.parametrize(
    "subject",
    [
        "feat: add standalone build",
        "fix(tui): restore result navigation",
        "build!: require Python 3.11",
        "chore(release)!: publish v0.1.0",
        "fixup! feat: add standalone build",
    ],
)
def test_accepts_conventional_commit_subject(subject: str) -> None:
    assert is_conventional_subject(subject)


@pytest.mark.parametrize(
    "subject",
    [
        "Add standalone build",
        "feature: add standalone build",
        "feat add standalone build",
        "feat:",
    ],
)
def test_rejects_non_conventional_commit_subject(subject: str) -> None:
    assert not is_conventional_subject(subject)
