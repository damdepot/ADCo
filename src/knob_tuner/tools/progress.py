"""Progress reporting for deterministic DCo phases."""

from __future__ import annotations

import datetime
import sys
from typing import Callable


def make_progress_callback(
    log_file: str | None = None, verbose: bool = False
) -> Callable[[str], None]:
    """Return a progress emitter; stage lines emit by default.

    Every message is printed to stdout (flushed) and appended to
    ``log_file`` when provided. ``verbose`` adds timestamped detail lines;
    the default (non-verbose) path still emits the one-line stage summary so
    pipeline progress is visible without ``-v``. A log-file write failure
    warns on stderr instead of passing silently.
    """

    def _emit(message: str) -> None:
        if verbose:
            line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        else:
            line = str(message)
        print(line, flush=True)
        if log_file:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as exc:
                print(
                    f"[progress] warning: cannot write log file {log_file}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    return _emit
