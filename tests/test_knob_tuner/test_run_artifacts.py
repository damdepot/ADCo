"""Unit tests for the run artifact helpers."""

import json
import re
from pathlib import Path

import pytest

from src.knob_tuner.contracts import RunManifest, TuningStatus
from src.knob_tuner.tools.run_artifacts import (
    application_code_hash,
    create_run_dir,
    new_run_id,
    write_artifact,
    write_manifest,
)


def test_new_run_id_format_and_uniqueness():
    pattern = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
    ids = {new_run_id() for _ in range(20)}
    for run_id in ids:
        assert pattern.match(run_id)
    assert len(ids) == 20


def test_create_run_dir_creates_directory(tmp_path: Path):
    run_dir = create_run_dir(str(tmp_path), "run-1")
    assert Path(run_dir).is_dir()
    assert Path(run_dir) == tmp_path / "run-1"


def test_create_run_dir_collision_raises(tmp_path: Path):
    create_run_dir(str(tmp_path), "run-1")
    with pytest.raises(FileExistsError):
        create_run_dir(str(tmp_path), "run-1")


def test_write_manifest_creates_json(tmp_path: Path):
    run_dir = create_run_dir(str(tmp_path), "run-1")
    manifest = RunManifest(
        run_id="run-1",
        timestamp="2026-01-01T00:00:00Z",
        status=TuningStatus.PASS,
    )

    path = write_manifest(run_dir, manifest)

    assert path == str(Path(run_dir) / "manifest.json")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["run_id"] == "run-1"
    assert data["status"] == "PASS"


def test_write_artifact_creates_json_and_sanitizes_name(tmp_path: Path):
    run_dir = create_run_dir(str(tmp_path), "run-1")

    path = write_artifact(run_dir, "paired_result", {"status": "PASS"})

    assert path == str(Path(run_dir) / "paired_result.json")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data == {"status": "PASS"}

    nested = write_artifact(run_dir, "nested/name", {"ok": True})
    assert Path(nested).parent == Path(run_dir)
    assert Path(nested).name == "nested_name.json"


def test_application_code_hash_is_stable(tmp_path: Path):
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "schema.sql").write_text("SELECT 1;\n", encoding="utf-8")

    first = application_code_hash(str(tmp_path))
    second = application_code_hash(str(tmp_path))

    assert first == second
    assert len(first) == 64


def test_application_code_hash_changes_with_content(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text("print('hello')\n", encoding="utf-8")
    before = application_code_hash(str(tmp_path))

    source.write_text("print('goodbye')\n", encoding="utf-8")
    after = application_code_hash(str(tmp_path))

    assert before != after


def test_application_code_hash_skips_ignored_dirs_and_extensions(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    base = application_code_hash(str(tmp_path))

    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "vendor.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not code\n", encoding="utf-8")

    assert application_code_hash(str(tmp_path)) == base


def test_application_code_hash_empty_dir_returns_empty(tmp_path: Path):
    assert application_code_hash(str(tmp_path)) == ""


def test_application_code_hash_missing_dir_returns_empty(tmp_path: Path):
    assert application_code_hash(str(tmp_path / "does-not-exist")) == ""
