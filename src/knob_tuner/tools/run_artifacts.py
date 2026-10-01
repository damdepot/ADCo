"""Run artifact helpers for the knob_tuner pipeline."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from src.knob_tuner.contracts import RunManifest, TuningStatus
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


def write_manifest(run_dir: str, manifest: RunManifest) -> str:
    """Write ``manifest.json`` into ``run_dir`` and return its path."""
    path = os.path.join(run_dir, "manifest.json")
    write_json_file(path, manifest.model_dump())
    return path


def build_run_manifest(state: Any) -> RunManifest:
    """Build a :class:`RunManifest` from session state (single source of truth).

    Both the in-graph ``finalize_node`` (``workflow.py``) and the
    post-runner fallback (``main._manifest_from_state``) delegate here so
    ``db_image``, ``errors``, ``status``, ``attempt_count``,
    ``verified_knobs``, ``pending_restart_knobs`` and ``validation_timings``
    agree no matter which path writes ``manifest.json``. Never raises for
    missing keys (falls back to ``INCONCLUSIVE``/empties); callers pass a
    plain state dict or any mapping with ``.get``.
    """
    from src.knob_tuner.tools.docker_tools import resolve_docker_image
    from src.knob_tuner.tools.knobs import coerce_profile

    get = getattr(state, "get", None)
    if not callable(get):
        state = {}
        get = state.get

    try:
        status = TuningStatus(str(get("result_status", TuningStatus.INCONCLUSIVE.value)).upper())
    except ValueError:
        status = TuningStatus.INCONCLUSIVE

    # Reconcile against the live apply: a validated PASS is only a success if
    # the production mutation actually happened. A failed, partial, or absent
    # apply must never surface as a PASS manifest. MANUAL_SQL is not an
    # applied success. Only a full APPLIED outcome preserves PASS.
    live = get("live_result") or {}
    if not isinstance(live, dict):
        live = {}
    live_status = str(live.get("status", "") or "").upper()
    errors = list(get("staging_issues", []) or [])
    if live_status in ("FAILED", "ERROR"):
        reason = str(live.get("reason", "") or "")
        errors.append(f"live apply {live_status}: {reason}".rstrip(": "))
    if status == TuningStatus.PASS and live_status != "APPLIED":
        if live_status in ("FAILED", "ERROR"):
            status = TuningStatus.FAIL
        elif live_status in ("APPLIED_NOTHING", "COMPLETED"):
            # Empty applies never yield PASS (COMPLETED is the legacy
            # empty-apply status): every-knob-failed is a FAIL, while
            # every-knob-skipped / --apply-mode none is INCONCLUSIVE.
            res = [r for r in (live.get("results") or []) if isinstance(r, dict)]
            if any(r.get("status") == "failed" for r in res):
                status = TuningStatus.FAIL
            else:
                status = TuningStatus.INCONCLUSIVE
            reason = str(live.get("reason", "") or "nothing applied")
            errors.append(f"live apply {live_status}: {reason}".rstrip(": "))
        else:
            # PARTIAL, SKIPPED, MANUAL_SQL, absent, ... : no full success.
            status = TuningStatus.INCONCLUSIVE
            if live_status == "PARTIAL":
                reason = str(live.get("reason", "") or "partial apply")
                errors.append(f"live apply {live_status}: {reason}".rstrip(": "))

    profile = coerce_profile(get("sysbench_profile"))
    target = get("target", "") or ""
    db_type = get("db_type", "") or ""
    db_version = get("db_version") or ""

    db_image = get("db_image") or ""
    if not db_image:
        try:
            db_image = resolve_docker_image(db_type, db_version)
        except Exception:
            db_image = ""

    attestation = get("validation_attestation") or {}
    if isinstance(attestation, dict):
        verified_knobs = attestation.get("verified_knobs", []) or []
    else:
        verified_knobs = getattr(attestation, "verified_knobs", []) or []

    # Attempt accounting lives in validation_attempt_count (screens +
    # compile rejections); experiment_history rows are screens only, so the
    # counter is authoritative and history length is only the fallback.
    try:
        attempt_count = int(get("validation_attempt_count", 0) or 0)
    except (TypeError, ValueError):
        attempt_count = 0
    if attempt_count <= 0:
        attempts = get("validation_attempts") or []
        attempt_count = len(attempts) if isinstance(attempts, list) else 0

    pending = live.get("pending_restart_knobs", []) or []
    timings = get("validation_timings") or {}
    if not isinstance(timings, dict):
        timings = {}

    return RunManifest(
        run_id=get("run_id", "") or "",
        timestamp=datetime.now(timezone.utc).isoformat(),
        status=status,
        resource_budget=get("resource_budget") or {},
        db_engine=db_type,
        db_version=str(db_version),
        db_image=db_image,
        application_target=target,
        application_code_hash=application_code_hash(target),
        knob_plan_hash=get("knob_plan_hash", "") or "",
        sysbench_profile_hash=profile.profile_hash(),
        seed=profile.seed,
        client_threads=profile.threads,
        attempt_count=attempt_count,
        applied_knobs=get("applied_knobs", []) or [],
        verified_knobs=verified_knobs,
        pending_restart_knobs=list(pending) if isinstance(pending, list) else [],
        validation_timings=dict(timings),
        errors=errors,
        final_status=status.value,
    )


def write_artifact(run_dir: str, name: str, data: Any) -> str:
    """Write ``<run_dir>/<name>.json`` and return its path.

    Path separators in ``name`` are sanitized so artifacts stay inside
    ``run_dir``.
    """
    safe_name = str(name).replace(os.sep, "_")
    if os.altsep:
        safe_name = safe_name.replace(os.altsep, "_")
    path = os.path.join(run_dir, f"{safe_name}.json")
    write_json_file(path, data)
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
