from __future__ import annotations

import asyncio
import shutil
import sys
from collections import deque
from contextlib import suppress
from dataclasses import replace

from .transfers import ResourceProgress

TERMINAL = {"complete", "skipped", "failed", "error", "removed"}
STATES = {
    "waiting": "排队",
    "active": "下载",
    "paused": "暂停",
    "retrying": "重试",
    "transferred": "整理",
    "complete": "完成",
    "skipped": "跳过",
    "failed": "失败",
    "error": "失败",
    "removed": "取消",
}


def size_text(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def bar(fraction: float | None, style: str, frame: int, width: int = 18) -> str:
    if fraction is None:
        return "|/-\\"[frame % 4] + " " * (width - 1)
    fill = max(0, min(width, int(width * fraction)))
    if style == "#":
        return "#" * fill + " " * (width - fill)
    if style == "ILoveCandy":
        return (
            " " * min(fill, width - 1)
            + ("C" if frame % 2 else "c")
            + "." * max(0, width - fill - 1)
        )
    return "=" * max(0, fill - 1) + (">" if fill else "") + " " * (width - fill)


class ProgressState:
    def __init__(self, total_songs: int) -> None:
        self.total_songs = total_songs
        self.completed_songs = 0
        self.resources: dict[str, ResourceProgress] = {}
        self.logs: deque[str] = deque()

    def update(self, event: ResourceProgress) -> None:
        previous = self.resources.get(event.resource_id)
        if event.status == "retrying" and previous is not None:
            event = replace(event, downloaded=previous.downloaded, total=previous.total)
        elif event.status in TERMINAL and event.total is None and previous is not None:
            # Completion/failure notices may omit length. Keep measured bytes for
            # unknown-length streams and partially transferred failed resources.
            event = replace(
                event, downloaded=max(event.downloaded, previous.downloaded), total=previous.total
            )
        self.resources[event.resource_id] = event
        if event.status in TERMINAL | {"retrying"} and (
            previous is None or (previous.status, previous.attempt) != (event.status, event.attempt)
        ):
            self.logs.append(
                f"{event.label}：{STATES.get(event.status, event.status)}"
                + (f" · {event.error}" if event.error else "")
            )

    @property
    def totals(self) -> tuple[int, int | None, float]:
        values = tuple(self.resources.values())
        downloaded = sum(event.downloaded for event in values)
        total = (
            sum(event.total or 0 for event in values)
            if values and all(event.total is not None for event in values)
            else None
        )
        speed = sum(event.speed for event in values if event.status == "active")
        return downloaded, total, speed

    def lines(self, style: str = "=>", frame: int = 0) -> list[str]:
        downloaded, total, speed = self.totals
        fraction = downloaded / total if total else None
        amount = (
            f"{size_text(downloaded)}/{size_text(total)}"
            if total is not None
            else size_text(downloaded)
        )
        percentage = f" {fraction:.0%}" if fraction is not None else ""
        eta = f" ETA {max(0, total - downloaded) / speed:.0f}s" if total and speed > 0 else ""
        lines = [
            f"[{bar(fraction, style, frame)}]{percentage} {amount} {size_text(speed)}/s{eta}"
            f" · 歌曲 {self.completed_songs}/{self.total_songs}"
        ]
        for event in self.resources.values():
            if event.status in TERMINAL:
                continue
            fraction = event.downloaded / event.total if event.total else None
            percentage = f" {fraction:.0%}" if fraction is not None else ""
            eta = (
                f" ETA {(event.total - event.downloaded) / event.speed:.0f}s"
                if (event.total and event.speed > 0 and event.status == "active")
                else ""
            )
            lines.append(
                f"{event.label} [{bar(fraction, style, frame)}]{percentage} "
                f"{size_text(event.downloaded)} {size_text(event.speed)}/s{eta} "
                f"{STATES.get(event.status, event.status)}"
            )
        return lines


class CliProgress:
    def __init__(self, state: ProgressState, style: str) -> None:
        self.state, self.style = state, style
        self.task: asyncio.Task | None = None
        self.frame = 0
        self._previous = ""
        self._rows = 0
        self._tty = sys.stderr.isatty()

    async def __aenter__(self) -> CliProgress:
        self.task = asyncio.create_task(self._refresh())
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.render()

    async def _refresh(self) -> None:
        while True:
            self.render()
            await asyncio.sleep(0.2)

    def render(self) -> None:
        lines = self.state.lines(self.style, self.frame)
        self.frame += 1
        logs = list(self.state.logs)
        self.state.logs.clear()
        if self._tty:
            terminal = shutil.get_terminal_size((80, 24))
            width = max(10, terminal.columns - 1)
            # Rich measures terminal cells (CJK titles occupy two cells) and strips control chars.
            from rich.console import Console
            from rich.text import Text

            if self._rows:
                sys.stderr.write(f"\x1b[{self._rows}A\x1b[J")
            console = Console(file=sys.stderr, force_terminal=True, width=width, highlight=False)
            for line in logs:
                console.print(Text(line), overflow="ellipsis", no_wrap=True)
            visible = lines[: max(1, terminal.lines - 3)]
            for line in visible:
                console.print(Text(line), overflow="ellipsis", no_wrap=True)
            self._rows = len(visible)
        else:
            # Keep log files bounded: periodic aggregate only, plus state transitions.
            summary = lines[0].split("] ", 1)[-1]
            if summary != self._previous:
                print(summary, file=sys.stderr)
                self._previous = summary
            for log in logs:
                print(log, file=sys.stderr)
        sys.stderr.flush()
