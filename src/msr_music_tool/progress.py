from __future__ import annotations

import asyncio
import shutil
import sys
from collections import deque
from contextlib import suppress
from dataclasses import replace

from rich.console import Console
from rich.text import Text

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
            return f"{value:.2f} {unit}"
        value /= 1024
    return ""


def amount_text(downloaded: int, total: int | None) -> str:
    return f"{size_text(downloaded)}/{size_text(total) if total is not None else '?'}"


def bar(fraction: float | None, frame: int, width: int) -> Text:
    result = Text()
    if fraction is None:
        # Unknown lengths pulse along the track instead of implying a percentage.
        pulse = min(3, width)
        start = frame % (width - pulse + 1)
        result.append("━" * start, "bright_black")
        result.append("━" * pulse, "green")
        result.append("━" * (width - start - pulse), "bright_black")
    else:
        scaled = width * max(0.0, min(1.0, fraction))
        fill = int(scaled)
        partial = int(scaled > fill)
        result.append("━" * fill, "green")
        result.append("╸" * partial, "green")
        result.append("━" * (width - fill - partial), "bright_black")
    return result


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

    @property
    def summary(self) -> str:
        downloaded, total, speed = self.totals
        return (
            f"歌曲 {self.completed_songs}/{self.total_songs}  {amount_text(downloaded, total)}"
            f"  {size_text(speed)}/s"
        )

    def lines(self, *, width: int = 80, frame: int = 0) -> list[Text]:
        downloaded, total, speed = self.totals
        rows = [(f"歌曲 {self.completed_songs}/{self.total_songs}", downloaded, total, speed)]
        for event in self.resources.values():
            if event.status in TERMINAL:
                continue
            label = event.label
            if event.status != "active":
                label += f" · {STATES.get(event.status, event.status)}"
            rows.append((label, event.downloaded, event.total, event.speed))

        width = max(1, width)
        amounts = [Text(amount_text(done, length)) for _label, done, length, _speed in rows]
        speeds = [Text(f"{size_text(speed)}/s", style="dim") for *_rest, speed in rows]
        speed_width = min(max(speed.cell_len for speed in speeds), max(1, width - 10))
        amount_width = min(
            max(amount.cell_len for amount in amounts), max(1, width - speed_width - 8)
        )
        name_width = max(1, min(24, width // 3, width - amount_width - speed_width - 12))
        bar_width = max(1, width - name_width - amount_width - speed_width - 6)
        lines = []
        for (label, done, length, _speed), amount, speed in zip(rows, amounts, speeds, strict=True):
            name = Text(label.replace("\n", " ").replace("\r", " ").expandtabs(1), style="dim")
            name.truncate(name_width, overflow="ellipsis", pad=True)
            amount.truncate(amount_width, overflow="ellipsis")
            amount.pad_left(amount_width - amount.cell_len)
            speed.truncate(speed_width, overflow="ellipsis")
            speed.pad_left(speed_width - speed.cell_len)
            fraction = done / length if length else (1.0 if length == 0 else None)
            line = Text.assemble(
                name, "  ", bar(fraction, frame, bar_width), "  ", amount, "  ", speed
            )
            line.truncate(width)
            lines.append(line)
        return lines


class CliProgress:
    def __init__(self, state: ProgressState) -> None:
        self.state = state
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
        logs = list(self.state.logs)
        self.state.logs.clear()
        if self._tty:
            terminal = shutil.get_terminal_size((80, 24))
            width = max(1, terminal.columns - 1)
            if self._rows:
                sys.stderr.write(f"\x1b[{self._rows}A\x1b[J")
            console = Console(file=sys.stderr, force_terminal=True, width=width, highlight=False)
            for line in logs:
                console.print(Text(line), overflow="ellipsis", no_wrap=True)
            visible = self.state.lines(width=width, frame=self.frame)[: max(1, terminal.lines - 3)]
            for line in visible:
                console.print(line, overflow="ellipsis", no_wrap=True)
            self._rows = len(visible)
        else:
            # Keep log files bounded: periodic aggregate only, plus state transitions.
            summary = self.state.summary
            if summary != self._previous:
                print(summary, file=sys.stderr)
                self._previous = summary
            for log in logs:
                print(log, file=sys.stderr)
        self.frame += 1
        sys.stderr.flush()
