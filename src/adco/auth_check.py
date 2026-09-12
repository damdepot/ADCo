"""Pre-flight auth health check: verify Google ADC can refresh a token."""
from __future__ import annotations

import sys
import time

import google.auth
from google.auth.transport.requests import Request

_ATTEMPTS = 3
_DELAY_S = 2.0


def check_auth() -> None:
    """Exit(1) if Google ADC credentials cannot refresh a token."""
    last: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            creds, _ = google.auth.default()
            creds.refresh(Request())
            print("Auth health check passed.")
            return
        except Exception as exc:  # DNS blips, missing/expired ADC
            last = exc
            if attempt < _ATTEMPTS:
                time.sleep(_DELAY_S * attempt)
    print(
        f"Auth health check FAILED after {_ATTEMPTS} attempts: {last}\n"
        "Check your network and ADC credentials "
        "(gcloud auth application-default login).",
        file=sys.stderr,
    )
    sys.exit(1)
