"""Verbose progress reporting for deterministic DCo phases."""

from __future__ import annotations

import datetime
from typing import Callable


def make_progress_callback(
    log_file: str | None = None, verbose: bool = False
) -> Callable[[str], None]:
    """Return a progress emitter; a no-op unless ``verbose`` is true.

    When enabled, each message is timestamped and written to stdout (flushed)
    and appended to ``log_file`` when provided.
    """
    if not verbose:

        def _silent(_message: str) -> None:
            return None

        return _silent

    def _emit(message: str) -> None:
        line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        print(line, flush=True)
        if log_file:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    return _emit
