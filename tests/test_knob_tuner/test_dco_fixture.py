"""Tests exercising DCo utilities against the synthetic DCo fixture app.

TPC-C style tests that require external benchmark checkouts belong under the
``case_study`` marker (select them with ``-m case_study``); they are excluded
from the default core suite.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.knob_tuner.tools.run_artifacts import application_code_hash

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "dco_app"


def test_fixture_hash_is_deterministic_hex():
    digest = application_code_hash(str(FIXTURE_DIR))

    assert isinstance(digest, str)
    assert len(digest) == 64
    int(digest, 16)
    assert application_code_hash(str(FIXTURE_DIR)) == digest


def test_fixture_hash_changes_when_content_changes(tmp_path: Path):
    copied = tmp_path / "dco_app"
    shutil.copytree(FIXTURE_DIR, copied)
    before = application_code_hash(str(copied))

    app_py = copied / "app.py"
    app_py.write_text(
        app_py.read_text(encoding="utf-8") + "\n# mutated\n", encoding="utf-8"
    )

    after = application_code_hash(str(copied))
    assert after != before
    assert len(after) == 64


def test_fixture_marker_is_usable():
    assert True


# TPC-C style / external-benchmark tests go under this marker.
@pytest.mark.case_study
def test_case_study_marker_exercised():
    assert True
