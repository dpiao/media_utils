"""In-place progress log coalescing for mediactl workers."""

from __future__ import annotations

import re
import time
from typing import Literal

LogMode = Literal["append", "replace"]
LogEntry = tuple[str, str, LogMode]

FILE_PROGRESS_INTERVAL_SEC = 5.0
FILE_PROGRESS_PCT_DELTA = 1.0


class LogCoalescer:
    """Collapse repeated progress lines (from \\r-based CLI output) into replace-last."""

    def __init__(self, pattern_strs: list[str]) -> None:
        self._patterns = [re.compile(p) for p in pattern_strs]
        self._prev_index: int | None = None

    def process(self, line: str) -> tuple[LogMode, bool]:
        idx = self._match_index(line)
        if idx is None:
            self._prev_index = None
            return "append", False
        if self._prev_index == idx:
            mode: LogMode = "replace"
        else:
            mode = "append"
        self._prev_index = idx
        return mode, True

    def _match_index(self, line: str) -> int | None:
        for i, pat in enumerate(self._patterns):
            if pat.match(line):
                return i
        return None


def progress_pct(line: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)%", line)
    if m:
        return float(m.group(1))
    m = re.search(r"Completed ([\d.]+) GiB/~?([\d.]+) GiB", line)
    if m:
        done, total = float(m.group(1)), float(m.group(2))
        if total > 0:
            return min(100.0, done / total * 100.0)
    return None


class ProgressFileThrottle:
    """Limit how often progress lines are appended to disk logs."""

    def __init__(self) -> None:
        self.pending: str | None = None
        self._last_write_at = 0.0
        self._last_pct: float | None = None

    def note_replace(self, line: str) -> str | None:
        self.pending = line
        now = time.monotonic()
        pct = progress_pct(line)
        if self._last_write_at == 0.0:
            return self._commit(line, now, pct)
        if now - self._last_write_at >= FILE_PROGRESS_INTERVAL_SEC:
            return self._commit(line, now, pct)
        if pct is not None and self._last_pct is not None:
            if abs(pct - self._last_pct) >= FILE_PROGRESS_PCT_DELTA:
                return self._commit(line, now, pct)
        return None

    def note_append_progress(self, line: str) -> str:
        self.pending = None
        now = time.monotonic()
        pct = progress_pct(line)
        self._commit(line, now, pct)
        return line

    def flush_before_normal(self) -> str | None:
        if self.pending is None:
            return None
        line = self.pending
        self.pending = None
        self._last_pct = None
        return line

    def _commit(self, line: str, now: float, pct: float | None) -> str:
        self._last_write_at = now
        self._last_pct = pct
        self.pending = line
        return line
