"""A file log for the resident processes.

`forge watch-transcripts` runs under `pythonw` from the Startup folder: it has no
stdout, so a loop error vanished without a trace. That is not a formatting
problem -- a scan error could repeat for weeks with nothing to look at, and
`doctor` only shows the instantaneous state.

The log is plain text, rotates at a fixed size, and never raises: a diagnostic
that can fail its own caller is worse than no diagnostic.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

MAX_LOG_BYTES = 1_000_000


def log_path() -> Path:
    from .config import runtime_dir

    return runtime_dir() / "runtime.log"


def report(message: str, *, dedupe_seconds: int = 0) -> None:
    """Print for an attached terminal and append to the runtime log.

    `dedupe_seconds` drops a repeat of the log's own last line when it is younger
    than that window. A duplicate launcher retries every 30 seconds and would
    otherwise fill the file with the same sentence.
    """
    print(message)
    if dedupe_seconds and _recent_repeat(log_path(), message, dedupe_seconds):
        return
    append(message)


def _recent_repeat(path: Path, message: str, window: int) -> bool:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 400))
            tail = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    lines = [line for line in tail.splitlines() if line.strip()]
    if not lines or message not in lines[-1]:
        return False
    try:
        stamped = time.mktime(time.strptime(lines[-1][:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return False
    return (time.time() - stamped) < window


def append(message: str) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} {message}\n")
    except OSError:
        # A log write must never be the reason a pass fails.
        pass


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= MAX_LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
