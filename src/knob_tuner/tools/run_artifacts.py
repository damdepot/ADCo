"""Run artifact helpers for the knob_tuner pipeline."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from src.knob_tuner.contracts import RunManifest
from src.knob_tuner.tools.file_tools import write_json_file

_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".sql",
        ".java",
        ".go",
        ".ts",
        ".js",
        ".rs",
        ".cpp",
        ".c",
        ".php",
        ".rb",
        ".cs",
    }
)

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        "dist",
        "build",
        "out",
    }
)


def new_run_id() -> str:
    """Return a unique run id: ``<UTC timestamp>-<8 hex chars>``."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def create_run_dir(base_dir: str, run_id: str) -> str:
    """Create ``<base_dir>/<run_id>`` and return the path.

    Raises:
        FileExistsError: If the target directory already exists.
    """
    path = os.path.join(base_dir, run_id)
    os.makedirs(path, exist_ok=False)
    return path


def write_json(path: str, data: Any) -> None:
    """Write ``data`` as pretty JSON, creating parent directories."""
    write_json_file(path, data)


def write_manifest(run_dir: str, manifest: RunManifest) -> str:
    """Write ``manifest.json`` into ``run_dir`` and return its path."""
    path = os.path.join(run_dir, "manifest.json")
    write_json(path, manifest.model_dump())
    return path


def write_artifact(run_dir: str, name: str, data: Any) -> str:
    """Write ``<run_dir>/<name>.json`` and return its path.

    Path separators in ``name`` are sanitized so artifacts stay inside
    ``run_dir``.
    """
    safe_name = str(name).replace(os.sep, "_")
    if os.altsep:
        safe_name = safe_name.replace(os.altsep, "_")
    path = os.path.join(run_dir, f"{safe_name}.json")
    write_json(path, data)
    return path


def application_code_hash(target_dir: str) -> str:
    """Return a deterministic sha256 over source files under ``target_dir``.

    Only known code extensions are hashed, and common build/cache
    directories are skipped. Returns an empty string when no files are found
    or the tree cannot be read.
    """
    try:
        relative_paths: list[str] = []
        for root, dirs, filenames in os.walk(target_dir):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
            for filename in filenames:
                if os.path.splitext(filename)[1].lower() in _CODE_EXTENSIONS:
                    full_path = os.path.join(root, filename)
                    relative_paths.append(os.path.relpath(full_path, target_dir))

        if not relative_paths:
            return ""

        hasher = hashlib.sha256()
        for relative_path in sorted(relative_paths):
            normalized = relative_path.replace(os.sep, "/")
            hasher.update(normalized.encode("utf-8"))
            hasher.update(b"\0")
            with open(os.path.join(target_dir, relative_path), "rb") as handle:
                hasher.update(handle.read())
            hasher.update(b"\0")
        return hasher.hexdigest()
    except Exception:
        return ""
