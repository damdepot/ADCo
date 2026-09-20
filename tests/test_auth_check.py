"""Tests for the pre-flight auth health check (no real ADC calls)."""

import pytest

from src.adco.auth_check import check_auth


# ---------------------------------------------------------------------------
# test helpers
# ---------------------------------------------------------------------------

class FakeCreds:
    """Fake ADC credentials; fails the first `failures` refreshes."""

    def __init__(self, failures: int = 0):
        self.failures = failures
        self.calls = 0

    def refresh(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("DNS blip")


def _patch(monkeypatch, creds):
    monkeypatch.setattr("src.adco.auth_check.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "src.adco.auth_check.google.auth.default", lambda: (creds, "fake-project")
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_check_auth_succeeds_first_try(monkeypatch):
    creds = FakeCreds()
    _patch(monkeypatch, creds)

    assert check_auth() is None
    assert creds.calls == 1


def test_check_auth_succeeds_after_transient_failure(monkeypatch):
    creds = FakeCreds(failures=1)
    _patch(monkeypatch, creds)

    assert check_auth() is None
    assert creds.calls == 2


def test_check_auth_exits_after_persistent_failure(monkeypatch):
    creds = FakeCreds(failures=99)
    _patch(monkeypatch, creds)

    with pytest.raises(SystemExit) as excinfo:
        check_auth()
    assert excinfo.value.code == 1
    assert creds.calls == 3
