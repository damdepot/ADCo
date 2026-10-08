"""CLI entry point for the ADCo knob_tuner pipeline.

Usage:
    uv run python -m src.knob_tuner <target_dir> [OPTIONS]

The resource contract (``--cpu-cores`` / ``--memory``) is mandatory and is
validated before any side effect (Docker, orphan cleanup, model calls). Each
run creates a run-scoped artifact directory under ``--results-dir`` containing
``manifest.json`` and ``result.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import dataclasses
import datetime
import json
import math
import os
import re
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.genai import types

from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from src.knob_tuner.agent import create_root_agent
from src.knob_tuner.contracts import (
    DEFAULT_EARLY_STOP_MIN_REPS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_SET_KNOBS,
    DEFAULT_MAX_WINNERS,
    DEFAULT_MEASURE_REPS,
    DEFAULT_MEASURE_SECONDS,
    DEFAULT_MEASURE_WARMUP_SECONDS,
    DEFAULT_MIN_IMPROVEMENT_PCT,
    DEFAULT_SUCCESS_CANDIDATES,
    ResourceBudget,
    RunManifest,
    SysbenchProfile,
    TuningStatus,
    get_early_stop_min_reps,
    get_measure_reps,
    get_measure_seconds,
    get_measure_warmup_seconds,
)
from src.knob_tuner.stages.models import normalize_workload_profile
from src.knob_tuner.tools.db_connector import DBConfig, load_db_config
from src.knob_tuner.tools.docker_tools import (
    ACTIVE_CONTAINERS,
    cleanup_orphan_containers,
    prune_staging_artifacts,
    stop_staging_db,
)
from src.knob_tuner.tools.run_artifacts import (
    create_run_dir,
    new_run_id,
    write_manifest,
)
from src.intent_analyzer.main import run_pipeline as run_intent_analyzer

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

DEFAULT_MODEL = "gemini-3.5-flash-lite"


def _process_cleanup() -> None:
    """Process-level cleanup hook to terminate any actively running staging containers."""
    containers = list(ACTIVE_CONTAINERS)
    for name in containers:
        try:
            stop_staging_db(name)
        except Exception:
            pass


def _signal_handler(signum: int, frame: Any) -> None:
    """Signal handler for SIGINT and SIGTERM."""
    _log_event(f"Received termination signal ({signum}). Cleaning up active containers...")
    _process_cleanup()
    sys.exit(128 + signum)


def register_cleanup_handlers() -> None:
    """Register process-level atexit and signal handlers for safe container teardown."""
    atexit.register(_process_cleanup)
    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except (ValueError, AttributeError):
        pass


# Automatically register on module import
register_cleanup_handlers()


def _maybe_parse(value: object) -> dict:
    """Return *value* as a dict, JSON-parsing strings (stripping markdown fences)."""
    if isinstance(value, str):
        stripped = re.sub(r"^```[a-z]*\n?", "", value.strip(), flags=re.MULTILINE)
        stripped = re.sub(r"```$", "", stripped.strip())
        try:
            return json.loads(stripped.strip())
        except (json.JSONDecodeError, ValueError):
            return {}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value if isinstance(value, dict) else {}


def _parse_budget(cpu_cores_arg: Any, memory_arg: Any) -> ResourceBudget:
    """Strictly parse the resource contract.

    Rejects ``None``, ``"auto"``, booleans, and non-positive/non-numeric values.
    No host auto-detection is performed.

    Raises:
        ValueError: If either value is missing or invalid.
    """

    def _reject_placeholder(value: Any, label: str) -> Any:
        if value is None:
            raise ValueError(
                f"--{label} is required; no default and no host auto-detection "
                "is performed."
            )
        if isinstance(value, bool):
            raise ValueError(f"--{label} must be a positive number, not a boolean.")
        if isinstance(value, str) and value.strip().lower() in ("", "auto"):
            raise ValueError(
                f"--{label} must be an explicit positive number; 'auto' is not "
                "supported."
            )
        return value

    cpu_raw = _reject_placeholder(cpu_cores_arg, "cpu-cores")
    memory_raw = _reject_placeholder(memory_arg, "memory")

    try:
        cpu_cores = int(str(cpu_raw).strip())
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid --cpu-cores value: {cpu_raw!r}. Must be a positive integer."
        ) from None
    if cpu_cores <= 0:
        raise ValueError(
            f"Invalid --cpu-cores value: {cpu_raw!r}. Must be a positive integer."
        )

    try:
        memory_gb = float(str(memory_raw).strip())
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid --memory value: {memory_raw!r}. Must be a positive number of GB."
        ) from None
    if not math.isfinite(memory_gb) or memory_gb <= 0:
        raise ValueError(
            f"Invalid --memory value: {memory_raw!r}. Must be a positive number of GB."
        )

    return ResourceBudget(cpu_cores=cpu_cores, memory_gb=memory_gb)


def _load_profile(source: str | None) -> SysbenchProfile:
    """Load a :class:`SysbenchProfile` from a JSON path (or return defaults).

    Raises:
        ValueError: If a supplied profile path is missing or invalid.
    """
    if source is None:
        return SysbenchProfile()
    path = os.path.abspath(source)
    if not os.path.isfile(path):
        raise ValueError(f"sysbench profile file not found: {path}")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid sysbench profile JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"sysbench profile at {path} must be a JSON object")
    try:
        return SysbenchProfile(**data)
    except Exception as exc:
        raise ValueError(f"invalid sysbench profile at {path}: {exc}") from exc


def _log_event(msg: str, log_file: str | None = None, verbose: bool = False) -> None:
    """Write log entry to file and optionally stdout."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"
    if verbose:
        print(formatted_msg)
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(formatted_msg + "\n")
        except Exception as exc:
            print(
                f"[{timestamp}] warning: cannot write log file {log_file}: {exc}"
            )


def build_parser() -> argparse.ArgumentParser:
    """Construct and return the argument parser for knob_tuner CLI."""
    parser = argparse.ArgumentParser(
        description="ADCo Knob Tuner — automated database configuration tuning pipeline",
    )
    parser.add_argument(
        "target",
        help="Path to the application codebase or target repository to analyze",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini model to use for all agents (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--db-type",
        choices=["postgres", "mysql"],
        default="postgres",
        help="Database engine type: 'postgres' or 'mysql' (default: postgres)",
    )
    parser.add_argument(
        "--db-name",
        required=True,
        help="Database name to connect and optimize",
    )
    parser.add_argument(
        "--cpu-cores",
        required=True,
        help="Number of CPU cores allocated for the target DB (required)",
    )
    parser.add_argument(
        "--memory",
        required=True,
        help="Database memory limit in GB (required)",
    )
    parser.add_argument(
        "--apply-mode",
        choices=[
            "none",
            "live",
            "manual",
        ],
        default="live",
        help=(
            "How validated knobs are applied: 'live' applies reloadable knobs "
            "now and persists restart-required knobs for the next restart "
            "(never auto-restarts); 'manual' emits SQL only and never mutates "
            "(default: live; legacy spellings dynamic, safe-auto, "
            "persist-static, maintenance-assisted still accepted)"
        ),
    )
    parser.add_argument(
        "--screen-total-rows",
        type=int,
        default=0,
        help=(
            "Total sysbench rows for the screening dataset; 0 derives it from the "
            "inspected target's row estimates (default: 0)"
        ),
    )
    parser.add_argument(
        "--screen-max-rows",
        type=int,
        default=5_000_000,
        help="Upper bound on the derived screening dataset size (default: 5000000)",
    )
    parser.add_argument(
        "--screening-benchmark",
        choices=["sysbench", "pgbench"],
        default="sysbench",
        help=(
            "Measurement used by the screening gate: 'sysbench' OLTP (default) or "
            "'pgbench' sort/hash analytical workload"
        ),
    )
    parser.add_argument(
        "--measure-reps",
        type=int,
        default=DEFAULT_MEASURE_REPS,
        help=(
            "Measurement reps per arm in single-fidelity mode: one "
            "shared baseline plus one arm per plan, each with this many runs "
            f"(default: {DEFAULT_MEASURE_REPS})"
        ),
    )
    parser.add_argument(
        "--measure-seconds",
        type=int,
        default=DEFAULT_MEASURE_SECONDS,
        help=f"Measured seconds per repetition (default: {DEFAULT_MEASURE_SECONDS})",
    )
    parser.add_argument(
        "--measure-warmup-seconds",
        type=int,
        default=DEFAULT_MEASURE_WARMUP_SECONDS,
        help=(
            "Warmup seconds per measurement arm "
            f"(default: {DEFAULT_MEASURE_WARMUP_SECONDS})"
        ),
    )
    parser.add_argument(
        "--early-stop-min-reps",
        type=int,
        default=DEFAULT_EARLY_STOP_MIN_REPS,
        help=(
            "Minimum reps before an arm may stop early for futility "
            "(95%% upper bound below zero); winners always run the full count "
            f"(default: {DEFAULT_EARLY_STOP_MIN_REPS})"
        ),
    )
    parser.add_argument(
        "--max-set-knobs",
        type=int,
        default=DEFAULT_MAX_SET_KNOBS,
        help=(
            "Maximum distinct knobs per experiment; larger sets are rejected "
            f"without spending a benchmark run (default: {DEFAULT_MAX_SET_KNOBS})"
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=(
            "Maximum screening attempts before the loop stops "
            f"(default: {DEFAULT_MAX_ATTEMPTS})"
        ),
    )
    parser.add_argument(
        "--success-candidates",
        type=int,
        default=DEFAULT_SUCCESS_CANDIDATES,
        help=(
            "Number of LCB-clearing winners to collect before a winner stop is "
            "allowed; a winner stop is premature while fewer have cleared the "
            f"win gate (default: {DEFAULT_SUCCESS_CANDIDATES})"
        ),
    )
    parser.add_argument(
        "--max-winners",
        type=int,
        default=None,
        help=(
            "Alias onto the same winner quota as --success-candidates: when "
            "given, it sets the number of certified winners to collect "
            f"(default: unset, falls back to --success-candidates; {DEFAULT_MAX_WINNERS} "
            "is the historical multi-winner default)"
        ),
    )
    parser.add_argument(
        "--min-improvement-pct",
        type=float,
        default=DEFAULT_MIN_IMPROVEMENT_PCT,
        help=(
            "Minimum LCB on throughput (percent) a candidate must exceed to "
            "count as a winner / clear the win gate "
            f"(default: {DEFAULT_MIN_IMPROVEMENT_PCT})"
        ),
    )
    parser.add_argument(
        "--results-dir",
        default="results/dco",
        help="Base directory for run-scoped artifacts (default: results/dco)",
    )
    parser.add_argument(
        "--db-config",
        default="db.config",
        help="Path to database connection config INI file (default: db.config)",
    )
    parser.add_argument(
        "--log-file",
        default="logs/knob_tuner.log",
        help="Path to execution log file (default: logs/knob_tuner.log)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate tuning process without applying modifications to live database",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed progress of each pipeline step",
    )
    parser.add_argument(
        "--buffer-time",
        type=float,
        default=0.0,
        help="Buffer sleep time in seconds before each LLM call (default: 0.0)",
    )
    return parser


def _redact_db_config(cfg: DBConfig) -> dict[str, Any]:
    """Return a JSON-serializable view of *cfg* with the password removed.

    Session state is persisted and may be sent to a remote session service, so
    secrets must never be stored there. The real :class:`DBConfig` is
    reconstructed from ``db_config_path`` by the nodes that connect.
    """
    redacted = dataclasses.asdict(cfg)
    redacted.pop("password", None)
    return redacted


def build_initial_state(
    target: str,
    db_type: str,
    budget: ResourceBudget,
    db_config_path: str,
    log_file: str,
    knob_path: str,
    dry_run: bool,
    db_name: str = "",
    profile: SysbenchProfile | None = None,
    run_id: str = "",
    run_dir: str = "",
    apply_mode: str = "live",
    screen_total_rows: int = 0,
    screen_max_rows: int = 5_000_000,
    screening_benchmark: str = "sysbench",
    measure_reps: int = DEFAULT_MEASURE_REPS,
    measure_seconds: int = DEFAULT_MEASURE_SECONDS,
    measure_warmup_seconds: int = DEFAULT_MEASURE_WARMUP_SECONDS,
    early_stop_min_reps: int = DEFAULT_EARLY_STOP_MIN_REPS,
    max_set_knobs: int = DEFAULT_MAX_SET_KNOBS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    success_candidates: int = DEFAULT_SUCCESS_CANDIDATES,
    min_improvement_pct: float = DEFAULT_MIN_IMPROVEMENT_PCT,
    max_winners: int | None = None,
) -> dict[str, Any]:
    """Construct the initial session state for the knob tuner workflow."""
    profile = profile or SysbenchProfile()
    # Unified winner quota: --max-winners maps onto the same quota as
    # --success-candidates (an explicit --max-winners wins); both state keys
    # carry the single value so every reader agrees.
    quota = max(
        1,
        int(
            max_winners
            if max_winners is not None
            else (success_candidates or DEFAULT_SUCCESS_CANDIDATES)
        ),
    )
    state: dict[str, Any] = {
        "target": target,
        "db_type": db_type,
        "database": db_name,
        "resource_budget": budget.model_dump(),
        "cpu_cores": budget.cpu_cores,
        "memory_gb": budget.memory_gb,
        "sysbench_profile": profile.model_dump(),
        "sysbench_profile_hash": profile.profile_hash(),
        "apply_mode": apply_mode,
        "run_id": run_id,
        "run_dir": run_dir,
        "db_config_path": db_config_path,
        "log_file": log_file,
        "knob_path": knob_path,
        "dry_run": dry_run,
        "validation_attempt_count": 0,
        "max_attempts": max(1, int(max_attempts or DEFAULT_MAX_ATTEMPTS)),
        "success_candidates": quota,
        "max_winners": quota,
        "min_improvement_pct": float(min_improvement_pct),
        "screen_total_rows": int(screen_total_rows),
        "screen_max_rows": int(screen_max_rows),
        "durability_profile": "strict",
        "screening_benchmark": str(screening_benchmark or "sysbench"),
        "workload_hint": "",
        "measure_reps": int(measure_reps),
        "measure_seconds": int(measure_seconds),
        "measure_warmup_seconds": int(measure_warmup_seconds),
        "early_stop_min_reps": int(early_stop_min_reps),
        "max_set_knobs": max(1, int(max_set_knobs or DEFAULT_MAX_SET_KNOBS)),
        # Staged-graph loop counters and the candidate/diagnosis context.
        "experiment_history": [],
        "rejected_history": [],
        "workload_profile": normalize_workload_profile(None, ""),
    }
    # Single source for the timing defaults + clamps (contracts is canonical).
    state["measure_reps"] = get_measure_reps(state)
    state["measure_seconds"] = get_measure_seconds(state)
    state["measure_warmup_seconds"] = get_measure_warmup_seconds(state)
    state["early_stop_min_reps"] = get_early_stop_min_reps(state)

    if os.path.isfile(db_config_path):
        try:
            cfg = load_db_config(db_config_path, db_type=db_type, db_override=db_name)
            redacted = _redact_db_config(cfg)
            state["db_config"] = redacted
            # R3: canonical-only rewrite (mirrors db_name/dbname deleted).
            state["database"] = cfg.database
        except Exception:
            pass

    return state


def _derive_status(state: dict[str, Any]) -> str:
    """Resolve the final tuning status from a state dict."""
    status = state.get("result_status")
    if not status:
        manifest = state.get("run_manifest") or {}
        if isinstance(manifest, dict):
            status = manifest.get("status") or manifest.get("final_status")
        elif hasattr(manifest, "status"):
            status = manifest.status.value if hasattr(manifest.status, "value") else manifest.status
    return str(status or "UNKNOWN").upper()


def _status_enum(state: dict[str, Any]) -> TuningStatus:
    """Resolve the final tuning status as a :class:`TuningStatus` enum."""
    try:
        return TuningStatus(_derive_status(state))
    except ValueError:
        return TuningStatus.INCONCLUSIVE


def _manifest_from_state(state: dict[str, Any]) -> RunManifest:
    """Build a :class:`RunManifest`, preferring one already in state.

    The construction itself lives in
    :func:`src.knob_tuner.tools.run_artifacts.build_run_manifest` (single
    source of truth shared with the in-graph ``finalize_node``); this
    fallback only mirrors it when the workflow never produced a manifest
    (e.g. a crash stub).
    """
    from src.knob_tuner.tools.run_artifacts import build_run_manifest

    raw = state.get("run_manifest")
    if isinstance(raw, RunManifest):
        return raw
    if isinstance(raw, dict) and raw:
        try:
            return RunManifest.model_validate(raw)
        except Exception:
            pass
    return build_run_manifest(state)


def _archive_winners(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the decision's ranked winners table (pure projection).

    Reads the ``winners`` rows recorded in ``candidate_archive`` by the
    decision node; missing keys degrade to an empty table, never throw.
    Generic: no knob names.
    """
    try:
        archive = state.get("candidate_archive") or {}
        if not isinstance(archive, dict):
            return []
        winners = archive.get("winners") or []
        return [dict(w) for w in winners if isinstance(w, dict)]
    except Exception:
        return []


def _family_coverage(state: dict[str, Any]) -> dict[str, Any]:
    """Count tried vs inventoried knob families (pure projection).

    The inventory (``knobs_info``) carries each knob's ``category`` family;
    tried knob names are collected from ``experiment_history`` rows (tolerant
    key reads plus a ``plan_hash`` join into ``candidates``/``all_rows``
    plans, plus the per-knob belief keys already attributed in state). No
    new measurement; missing keys degrade to empty counts, never throw.
    Generic: no knob names.
    """
    try:
        inventory = state.get("knobs_info") or state.get("available_knobs") or []
        if not isinstance(inventory, list):
            inventory = []
        family_of: dict[str, str] = {}
        inventory_counts: dict[str, int] = {}
        for entry in inventory:
            try:
                if isinstance(entry, dict):
                    name = str(entry.get("name") or "").strip().lower()
                    family = str(entry.get("category") or "").strip() or "uncategorized"
                elif hasattr(entry, "model_dump"):
                    dumped = entry.model_dump()
                    name = str(dumped.get("name") or "").strip().lower()
                    family = str(dumped.get("category") or "").strip() or "uncategorized"
                else:
                    name = str(getattr(entry, "name", "") or "").strip().lower()
                    family = str(getattr(entry, "category", "") or "").strip() or "uncategorized"
            except Exception:
                continue
            if not name:
                continue
            family_of[name] = family
            inventory_counts[family] = inventory_counts.get(family, 0) + 1

        def _names_from_plan(plan: Any) -> list[str]:
            try:
                knobs = plan.get("knobs") if isinstance(plan, dict) else getattr(plan, "knobs", None)
                if not isinstance(knobs, list):
                    return []
                return [
                    str(spec.get("name") or "").strip().lower()
                    for spec in knobs
                    if isinstance(spec, dict) and spec.get("name")
                ]
            except Exception:
                return []

        plans_by_hash: dict[str, Any] = {}
        for key in ("candidates", "all_rows"):
            try:
                for entry in state.get(key) or []:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("plan_hash") and entry.get("plan") is not None:
                        plans_by_hash.setdefault(str(entry["plan_hash"]), entry["plan"])
            except Exception:
                continue

        tried: set[str] = set()
        try:
            history = state.get("experiment_history") or []
            for row in history if isinstance(history, list) else []:
                if not isinstance(row, dict):
                    continue
                for hkey in ("knobs", "knob_names", "verified_knobs", "targets"):
                    try:
                        raw = row.get(hkey) or []
                        for item in raw if isinstance(raw, list) else []:
                            name = str(item.get("name") if isinstance(item, dict) else item or "").strip().lower()
                            if name:
                                tried.add(name)
                    except Exception:
                        continue
                plan = row.get("plan")
                if plan is not None:
                    tried.update(_names_from_plan(plan))
                try:
                    phash = row.get("plan_hash")
                except AttributeError:
                    phash = None
                if phash and phash in plans_by_hash:
                    tried.update(_names_from_plan(plans_by_hash[str(phash)]))
        except Exception:
            pass
        try:
            beliefs = state.get("knob_beliefs") or {}
            if isinstance(beliefs, dict):
                for name in beliefs:
                    if str(name or "").strip():
                        tried.add(str(name).strip().lower())
        except Exception:
            pass

        tried_counts: dict[str, int] = {}
        for name in tried:
            family = family_of.get(name, "uncategorized")
            tried_counts[family] = tried_counts.get(family, 0) + 1
        per_category = {
            family: {"inventory": inventory_counts.get(family, 0), "tried": tried_counts.get(family, 0)}
            for family in sorted(set(inventory_counts) | set(tried_counts))
        }
        return {
            "per_category": per_category,
            "categories_covered": sum(1 for counts in per_category.values() if counts["tried"] > 0),
            "categories_total": len(per_category),
            "knobs_tried": len(tried),
            "knobs_inventory": len(family_of),
        }
    except Exception:
        return {
            "per_category": {},
            "categories_covered": 0,
            "categories_total": 0,
            "knobs_tried": 0,
            "knobs_inventory": 0,
        }


def _tuning_gate_raw(state: dict[str, Any]) -> float | None:
    """Return the configured win-gate pct, or None when unrecorded.

    Pure projection: state ``min_improvement_pct`` first, then the decision
    archive's copy; missing/non-numeric degrades to None, never throws.
    Generic: no knob names.
    """
    try:
        if not isinstance(state, dict):
            return None
        raw = state.get("min_improvement_pct")
        if raw is None:
            archive = state.get("candidate_archive") or {}
            if isinstance(archive, dict):
                raw = archive.get("min_improvement_pct")
        if raw is None:
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None
    except Exception:
        return None


def _tuning_applied(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Project applied knobs as ``[{knob, value}]`` (pure; never throws).

    Reads ``live_result.applied_knobs`` with the top-level ``applied_knobs``
    fallback (mirroring the CLI); entries accept ``knob``/``name`` keys.
    Generic: no knob names.
    """
    try:
        if not isinstance(state, dict):
            return []
        live = _maybe_parse(state.get("live_result", {}))
        if not isinstance(live, dict):
            live = {}
        raw = live.get("applied_knobs") or state.get("applied_knobs") or []
        if not isinstance(raw, list):
            return []
        projected: list[dict[str, Any]] = []
        for entry in raw:
            try:
                if isinstance(entry, dict):
                    name = entry.get("knob") or entry.get("name") or ""
                    value = entry.get("value", "")
                else:
                    name = str(entry or "")
                    value = ""
                name = str(name or "").strip()
                if not name:
                    continue
                projected.append({"knob": name, "value": value})
            except Exception:
                continue
        return projected
    except Exception:
        return []


def _build_tuning_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Project the end-of-run tuning summary for result.json (pure).

    Never throws; missing keys degrade to zero/empty counts (the gate falls
    back to the canonical default). Generic: no knob names.
    """
    try:
        if not isinstance(state, dict):
            state = {}
        try:
            winners = _archive_winners(state)
        except Exception:
            winners = []
        try:
            history = state.get("experiment_history")
            hist_rows = list(history) if isinstance(history, list) else []
        except Exception:
            hist_rows = []
        try:
            rejected = state.get("rejected_history")
            rejected_len = len(rejected) if isinstance(rejected, list) else 0
        except Exception:
            rejected_len = 0
        try:
            attempts_total = max(
                int(state.get("validation_attempt_count") or 0),
                len(hist_rows) + rejected_len,
            )
        except (TypeError, ValueError):
            attempts_total = len(hist_rows) + rejected_len
        except Exception:
            attempts_total = 0
        try:
            passed = sum(
                1
                for row in hist_rows
                if isinstance(row, dict)
                and str(row.get("status", "")).upper() == "PASS"
                and bool(row.get("confirmed", False))
            )
            if not hist_rows and winners:
                passed = len(winners)
        except Exception:
            passed = 0
        try:
            certified_count = sum(
                1 for w in winners if isinstance(w, dict) and bool(w.get("certified", False))
            )
        except Exception:
            certified_count = 0
        gate = _tuning_gate_raw(state)
        if gate is None:
            try:
                from src.knob_tuner.contracts import DEFAULT_MIN_IMPROVEMENT_PCT

                gate = float(DEFAULT_MIN_IMPROVEMENT_PCT)
            except Exception:
                gate = 5.0
        try:
            conf_by_round = _diagnosis_confidences_by_round(state)
        except Exception:
            conf_by_round = []
        try:
            hist_index_by_name: dict[str, int] = {}
            for idx, row in enumerate(hist_rows):
                if not isinstance(row, dict):
                    continue
                for key in ("arm", "name", "exp_name"):
                    label = str(row.get(key) or "").strip()
                    if label and label not in hist_index_by_name:
                        hist_index_by_name[label] = idx
                        break
            llm_confidences: list[float | None] = []
            for w in winners:
                try:
                    wname = str(w.get("name") or "").strip() if isinstance(w, dict) else ""
                except Exception:
                    wname = ""
                idx = hist_index_by_name.get(wname) if wname else None
                if idx is not None and 0 <= idx < len(conf_by_round):
                    llm_confidences.append(conf_by_round[idx])
                else:
                    llm_confidences.append(None)
            winner_llm_confidence: float | None = None
            if winners and llm_confidences:
                try:
                    rank1_pos = None
                    for pos, w in enumerate(winners):
                        if isinstance(w, dict) and w.get("rank") is not None:
                            try:
                                if int(w.get("rank")) == 1:
                                    rank1_pos = pos
                                    break
                            except (TypeError, ValueError):
                                continue
                    if rank1_pos is not None and rank1_pos < len(llm_confidences):
                        winner_llm_confidence = llm_confidences[rank1_pos]
                    else:
                        winner_llm_confidence = llm_confidences[0]
                except Exception:
                    winner_llm_confidence = None
        except Exception:
            llm_confidences = []
            winner_llm_confidence = None
        return {
            "attempts_total": attempts_total,
            "screens_measured": len(hist_rows),
            "passed": passed,
            "certified_count": certified_count,
            "applied": _tuning_applied(state),
            "gate_pct": gate,
            "llm_confidences": llm_confidences,
            "winner_llm_confidence": winner_llm_confidence,
        }
    except Exception:
        return {
            "attempts_total": 0,
            "screens_measured": 0,
            "passed": 0,
            "certified_count": 0,
            "applied": [],
            "gate_pct": 5.0,
            "llm_confidences": [],
            "winner_llm_confidence": None,
        }


def _fmt_pct_or_na(value: Any) -> str:
    """Format a pct number as ``+x.xx%``, else ``n/a`` (never throws)."""
    try:
        if value is None or isinstance(value, bool):
            return "n/a"
        number = float(value)
        if number != number:
            return "n/a"
        return f"{number:+.2f}%"
    except (TypeError, ValueError):
        return "n/a"
    except Exception:
        return "n/a"


def _fmt_count_or_na(value: Any) -> str:
    """Format an int count, else ``n/a`` (never throws)."""
    try:
        if value is None or isinstance(value, bool):
            return "n/a"
        return str(int(value))
    except (TypeError, ValueError):
        return "n/a"
    except Exception:
        return "n/a"


def _diag_confidence_raw(entry: Any) -> float | None:
    """Extract a 0..1 diagnosis confidence from one history entry (never throws)."""
    try:
        if entry is None or isinstance(entry, bool):
            return None
        raw: Any = None
        if isinstance(entry, dict):
            raw = entry.get("confidence")
        elif hasattr(entry, "model_dump"):
            try:
                raw = entry.model_dump().get("confidence")
            except Exception:
                raw = None
        else:
            raw = getattr(entry, "confidence", None)
        if raw is None or isinstance(raw, bool):
            return None
        conf = float(raw)
        if conf != conf or not (0.0 <= conf <= 1.0):
            return None
        return conf
    except (TypeError, ValueError):
        return None
    except Exception:
        return None


def _diagnosis_confidences_by_round(state: dict[str, Any]) -> list[float | None]:
    """Return per-round diagnosis confidences aligned to experiment_history.

    Join rule: experiment_history row ``i`` joins to diagnosis_history entry
    ``i`` by round order (0-based index); extra entries on either side are
    ignored and missing/invalid entries yield ``None``. Never throws.
    Generic: no knob names.
    """
    try:
        if not isinstance(state, dict):
            return []
        hist_raw = state.get("experiment_history")
        hist_rows = list(hist_raw) if isinstance(hist_raw, list) else []
        diag_raw = state.get("diagnosis_history")
        diag_rows = list(diag_raw) if isinstance(diag_raw, list) else []
        confidences: list[float | None] = []
        for idx in range(len(hist_rows)):
            if idx < len(diag_rows):
                confidences.append(_diag_confidence_raw(diag_rows[idx]))
            else:
                confidences.append(None)
        return confidences
    except Exception:
        return []


def _fmt_conf_or_na(value: Any) -> str:
    """Format a 0..1 confidence as ``0.00``-style, else ``n/a`` (never throws)."""
    try:
        if value is None or isinstance(value, bool):
            return "n/a"
        number = float(value)
        if number != number or not (0.0 <= number <= 1.0):
            return "n/a"
        return f"{number:.2f}"
    except (TypeError, ValueError):
        return "n/a"
    except Exception:
        return "n/a"


def _print_tuning_results(state: dict[str, Any]) -> None:
    """Print the ``=== Tuning Results ===`` end-of-run summary.

    Attempts (measured screens vs total attempts incl. cheap rejections),
    confirmed-PASS / certified counts, a capped rank table built from
    ``winners[]`` plus failed arms from ``experiment_history``, the final
    applied knobs, and a 3-line mean/LCB/LLM-conf gloss. Missing keys render as
    ``n/a``; never throws. Generic: no knob names.
    """
    try:
        if not isinstance(state, dict):
            state = {}
        try:
            winners = _archive_winners(state)
            if not isinstance(winners, list):
                winners = []
        except Exception:
            winners = []
        try:
            raw_attempt = state.get("validation_attempt_count")
            attempts = int(raw_attempt) if raw_attempt is not None else None
        except (TypeError, ValueError):
            attempts = None
        except Exception:
            attempts = None
        try:
            hist_raw = state.get("experiment_history")
            hist_rows = list(hist_raw) if isinstance(hist_raw, list) else None
        except Exception:
            hist_rows = None
        screens = len(hist_rows) if hist_rows is not None else None
        if hist_rows is not None:
            try:
                passed = sum(
                    1
                    for row in hist_rows
                    if isinstance(row, dict)
                    and str(row.get("status", "")).upper() == "PASS"
                    and bool(row.get("confirmed", False))
                )
            except Exception:
                passed = 0
        elif winners:
            passed = len(winners)
        else:
            passed = None
        gate = _tuning_gate_raw(state)
        if winners:
            try:
                certified = sum(
                    1
                    for w in winners
                    if isinstance(w, dict) and bool(w.get("certified", False))
                )
            except Exception:
                certified = 0
        elif hist_rows is not None:
            certified = 0
        else:
            certified = None

        print("\n=== Tuning Results ===")
        print(
            f"Attempts tried:        {_fmt_count_or_na(screens)} measured screens / "
            f"{_fmt_count_or_na(attempts)} total attempts (incl. cheap rejections)"
        )
        gate_s = f"{gate:.1f}%" if gate is not None else "n/a"
        print(
            f"Successful candidates: {_fmt_count_or_na(passed)} confirmed PASS; "
            f"Certified: {_fmt_count_or_na(certified)} (LCB > {gate_s})"
        )

        hist_by_name: dict[str, dict[str, Any]] = {}
        hist_index_by_name: dict[str, int] = {}
        if hist_rows is not None:
            for idx, row in enumerate(hist_rows):
                if not isinstance(row, dict):
                    continue
                for key in ("arm", "name", "exp_name"):
                    label = str(row.get(key) or "").strip()
                    if label:
                        hist_by_name.setdefault(label, row)
                        if label not in hist_index_by_name:
                            hist_index_by_name[label] = idx
                        break
        try:
            conf_by_round = _diagnosis_confidences_by_round(state)
        except Exception:
            conf_by_round = []
        def _conf_for_hist_idx(idx: Any) -> str:
            try:
                if idx is None or isinstance(idx, bool):
                    return "n/a"
                pos = int(idx)
                if 0 <= pos < len(conf_by_round):
                    return _fmt_conf_or_na(conf_by_round[pos])
                return "n/a"
            except (TypeError, ValueError):
                return "n/a"
            except Exception:
                return "n/a"
        listed: set[str] = set()
        table: list[dict[str, str]] = []
        for w in winners:
            if not isinstance(w, dict):
                continue
            name = str(w.get("name") or "").strip() or "n/a"
            hist = hist_by_name.get(name, {})
            status = str(hist.get("status", "") or "").upper() if hist else "PASS"
            if not status:
                status = "n/a"
            rank_raw = w.get("rank")
            try:
                rank_s = str(int(rank_raw)) if rank_raw is not None else "-"
            except (TypeError, ValueError):
                rank_s = "-"
            except Exception:
                rank_s = "-"
            try:
                n_knobs = w.get("n_knobs")
                if n_knobs is None and hist:
                    n_knobs = hist.get("n_knobs")
                knobs_s = str(int(n_knobs)) if n_knobs is not None else "n/a"
            except (TypeError, ValueError):
                knobs_s = "n/a"
            except Exception:
                knobs_s = "n/a"
            table.append(
                {
                    "rank": rank_s,
                    "name": name,
                    "knobs": knobs_s,
                    "mean": _fmt_pct_or_na(w.get("mean")),
                    "lcb": _fmt_pct_or_na(w.get("lcb")),
                    "status": status,
                    "certified": "yes" if bool(w.get("certified", False)) else "no",
                    # Join rule: experiment_history row i joins to
                    # diagnosis_history entry i by round order (index-based,
                    # length-mismatch tolerant); missing/invalid renders n/a.
                    "llm_conf": _conf_for_hist_idx(hist_index_by_name.get(name)),
                }
            )
            listed.add(name)
        if hist_rows is not None:
            for pos, row in enumerate(hist_rows):
                if not isinstance(row, dict):
                    continue
                if str(row.get("status", "")).upper() == "PASS":
                    continue
                name = ""
                for key in ("arm", "name", "exp_name"):
                    label = str(row.get(key) or "").strip()
                    if label:
                        name = label
                        break
                if not name:
                    name = "n/a"
                if name in listed:
                    continue
                listed.add(name)
                try:
                    knobs_s = (
                        str(int(row.get("n_knobs")))
                        if row.get("n_knobs") is not None
                        else "n/a"
                    )
                except (TypeError, ValueError):
                    knobs_s = "n/a"
                except Exception:
                    knobs_s = "n/a"
                status = str(row.get("status", "") or "").upper() or "n/a"
                table.append(
                    {
                        "rank": "-",
                        "name": name,
                        "knobs": knobs_s,
                        "mean": _fmt_pct_or_na(row.get("mean_delta_pct")),
                        "lcb": _fmt_pct_or_na(row.get("lcb_pct")),
                        "status": status,
                        "certified": "no",
                        # Join rule: experiment_history row i joins to
                        # diagnosis_history entry i by round order
                        # (index-based, length-mismatch tolerant).
                        "llm_conf": _conf_for_hist_idx(pos),
                    }
                )
        table = table[:10]
        print("Candidates (top 10): rank, name, knobs, mean%, LCB%, status, certified, LLM conf")
        if table:
            for row in table:
                print(
                    f"  {row['rank']:<4} {row['name']:<24} {row['knobs']:<5} "
                    f"{row['mean']:<9} {row['lcb']:<9} {row['status']:<6} {row['certified']:<8} {row.get('llm_conf', 'n/a')}"
                )
        else:
            print("  n/a (no candidates recorded)")

        applied = _tuning_applied(state)
        print("Final knobs applied:")
        if applied:
            for spec in applied:
                print(f"  {spec.get('knob')} = {spec.get('value')}")
        else:
            print("  none")
        print("mean% = average measured speedup vs baseline;")
        print("LCB% = pessimistic bound (95% sure the true gain is at least this).")
        print("LLM conf = diagnosing agent's confidence in its round assessment (0-1), not a success probability.")
    except Exception:
        pass


def _write_output_result(output_path: str, state: dict[str, Any]) -> None:
    """Serialize the combined tuning outcome to *output_path*."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    db_inspector_out = _maybe_parse(
        state.get("db_inspector_output", state.get("intent_analyzer_output", {}))
    )
    recommender_out = _maybe_parse(state.get("knob_recommender_output", {}))
    validation_out = _maybe_parse(state.get("validation_attestation", {}))
    live_out = _maybe_parse(state.get("live_result", {}))

    result_data = {
        "timestamp": datetime.datetime.now().isoformat(),
        "target": state.get("target"),
        "run_id": state.get("run_id"),
        "run_dir": state.get("run_dir"),
        "db_type": state.get("db_type"),
        # R3: result.json keeps the external "db_name" field name, sourced
        # from the canonical state key (mirrors deleted).
        "db_name": state.get("database"),
        "resource_budget": state.get("resource_budget"),
        "apply_mode": state.get("apply_mode"),
        "dry_run": state.get("dry_run", False),
        "staging_validated": state.get("staging_validated", False),
        "status": _derive_status(state),
        "validation_attempt_count": state.get("validation_attempt_count", 0),
        "validation_attempts": state.get("validation_attempts", []),
        "staging_issues": state.get("staging_issues", []),
        "run_manifest": state.get("run_manifest"),
        "intent_analyzer_output": db_inspector_out,
        "knob_recommender_output": recommender_out,
        "validation_attestation": validation_out,
        "live_result": live_out,
        "winners": _archive_winners(state),
        "family_coverage": _family_coverage(state),
        "tuning_summary": _build_tuning_summary(state),
        "outputs": {
            "db_inspector": db_inspector_out,
            "intent_analyzer": db_inspector_out,
            "knob_recommender": recommender_out,
            "validation": validation_out,
            "live": live_out,
        },
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result_data, f, indent=2, default=str)


def _refresh_latest_pointer(
    results_dir: str,
    run_dir: str,
    log_file: str | None = None,
    verbose: bool = False,
) -> None:
    """Best-effort refresh of ``<results_dir>/latest`` -> ``run_dir``.

    Never raises: platforms without symlink support (or a pre-existing
    ``latest`` that cannot be replaced) only yield a logged warning.
    """
    link = os.path.join(results_dir, "latest")
    try:
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(run_dir, link)
    except OSError as exc:
        _log_event(
            f"Could not refresh latest pointer {link}: {exc}",
            log_file=log_file,
            verbose=verbose,
        )


async def run_pipeline(
    target: str,
    model: str = DEFAULT_MODEL,
    db_type: str = "postgres",
    cpu_cores_arg: Any = None,
    memory_arg: Any = None,
    db_config: str = "db.config",
    log_file: str = "logs/knob_tuner.log",
    dry_run: bool = False,
    verbose: bool = False,
    db_name: str = "",
    buffer_time: float = 0.0,
    apply_mode: str = "live",
    results_dir: str = "results/dco",
    extra_initial_state: dict[str, Any] | None = None,
    screen_total_rows: int = 0,
    screen_max_rows: int = 5_000_000,
    screening_benchmark: str = "sysbench",
    measure_reps: int = DEFAULT_MEASURE_REPS,
    measure_seconds: int = DEFAULT_MEASURE_SECONDS,
    measure_warmup_seconds: int = DEFAULT_MEASURE_WARMUP_SECONDS,
    early_stop_min_reps: int = DEFAULT_EARLY_STOP_MIN_REPS,
    max_set_knobs: int = DEFAULT_MAX_SET_KNOBS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    success_candidates: int = DEFAULT_SUCCESS_CANDIDATES,
    min_improvement_pct: float = DEFAULT_MIN_IMPROVEMENT_PCT,
    max_winners: int | None = None,
) -> dict[str, Any]:
    """Execute the knob tuner pipeline using the ADK Runner and session service."""
    # 1. Resource contract FIRST: fail before any side effect.
    budget = _parse_budget(cpu_cores_arg, memory_arg)
    # Sysbench profile always defaults (pareto rand_type); durability always
    # strict; unconfirmed plans are never applied; orphans always cleaned up.
    # The win-gate pct is the one profile field exposed on the CLI.
    profile = SysbenchProfile(min_improvement_pct=float(min_improvement_pct))

    target_abs = os.path.abspath(target)
    db_config_abs = os.path.abspath(db_config)
    log_file_abs = os.path.abspath(log_file)
    results_dir_abs = os.path.abspath(results_dir)

    os.makedirs(os.path.dirname(log_file_abs), exist_ok=True)
    os.makedirs(results_dir_abs, exist_ok=True)

    # 2. Run-scoped artifacts.
    run_id = new_run_id()
    run_dir = create_run_dir(results_dir_abs, run_id)
    # Knob artifacts live inside the run directory (single source of truth).
    knob_path_abs = os.path.join(run_dir, "knobs")
    os.makedirs(knob_path_abs, exist_ok=True)

    try:
        orphans = cleanup_orphan_containers()
        if orphans:
            # cleanup_orphan_containers returns an int count; tolerate a list too.
            count = orphans if isinstance(orphans, int) else len(orphans)
            _log_event(
                f"Cleaned up {count} stale orphan container(s)",
                log_file=log_file_abs,
                verbose=verbose,
            )
    except Exception as exc:
        _log_event(f"Orphan cleanup warning: {exc}", log_file=log_file_abs, verbose=verbose)

    # Broad best-effort prune of prior runs' containers, snapshot images and
    # dangling volumes. Skipped in dry-run mode since it mutates the host.
    if not dry_run:
        try:
            pruned = prune_staging_artifacts()
            _log_event(
                f"Pruned {pruned.get('containers', 0)} stale container(s), "
                f"{pruned.get('images', 0)} snapshot image(s), "
                f"{pruned.get('volumes', 0)} dangling volume(s)",
                log_file=log_file_abs,
                verbose=verbose,
            )
        except Exception as exc:
            _log_event(
                f"Staging prune warning: {exc}", log_file=log_file_abs, verbose=verbose
            )

    initial_state = build_initial_state(
        target=target_abs,
        db_type=db_type,
        budget=budget,
        db_config_path=db_config_abs,
        log_file=log_file_abs,
        knob_path=knob_path_abs,
        dry_run=dry_run,
        db_name=db_name,
        profile=profile,
        run_id=run_id,
        run_dir=run_dir,
        apply_mode=apply_mode,
        screen_total_rows=screen_total_rows,
        screen_max_rows=screen_max_rows,
        screening_benchmark=screening_benchmark,
        measure_reps=measure_reps,
        measure_seconds=measure_seconds,
        measure_warmup_seconds=measure_warmup_seconds,
        early_stop_min_reps=early_stop_min_reps,
        max_set_knobs=max_set_knobs,
        max_attempts=max_attempts,
        success_candidates=success_candidates,
        min_improvement_pct=min_improvement_pct,
        max_winners=max_winners,
    )

    initial_state["verbose"] = verbose

    if extra_initial_state:
        initial_state.update(extra_initial_state)

    # Standalone fallback: if workload info is not provided, run intent_analyzer.
    if "workload_info" not in initial_state and "intent_output" not in initial_state:
        _log_event(
            f"Workload info not in state; running intent_analyzer on {target_abs}",
            log_file=log_file_abs,
            verbose=verbose,
        )
        try:
            intent_state = await run_intent_analyzer(
                target=target_abs,
                model=model,
                log_file=log_file_abs,
                verbose=verbose,
            )
            workload = intent_state.get("workload_info")
            if workload:
                initial_state["workload_info"] = workload
        except Exception as exc:
            _log_event(f"Intent analyzer warning: {exc}", log_file=log_file_abs, verbose=verbose)

    # The staged graph reads a merged workload_profile (never the raw keys).
    initial_state["workload_profile"] = normalize_workload_profile(
        initial_state.get("workload_info"), ""
    )

    session_service = InMemorySessionService()
    sid = uuid.uuid4().hex[:12]
    app_name = "knob_tuner"

    await session_service.create_session(
        app_name=app_name,
        user_id="pipeline",
        session_id=sid,
        state=initial_state,
    )

    if isinstance(model, str):
        resilient_model = Gemini(
            model=model,
            retry_options=types.HttpRetryOptions(
                initial_delay=1, attempts=5, exp_base=2
            ),
        )
    else:
        resilient_model = model

    agent = create_root_agent(model=resilient_model, buffer_time=buffer_time)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)

    user_message = (
        f"Tune database configuration knobs for the codebase at: {target_abs}\n\n"
        f"Configuration details:\n"
        f"- Database Type: {db_type}\n"
        f"- Database Name: {db_name}\n"
        f"- CPU Cores: {budget.cpu_cores}\n"
        f"- Memory: {budget.memory_gb} GB\n"
        f"- Apply Mode: {apply_mode}\n"
        f"- Run ID: {run_id}\n"
        f"- Run Directory: {run_dir}\n"
        f"- Database Config: {db_config_abs}\n"
        f"- Dry Run: {dry_run}\n"
        f"- Knob Path: {knob_path_abs}\n\n"
        f"The resource budget and run directory are already in session state."
    )

    _log_event(
        f"Starting knob_tuner pipeline (model={model}, db_type={db_type}, "
        f"cores={budget.cpu_cores}, mem={budget.memory_gb}GB, dry_run={dry_run}, "
        f"run_id={run_id})",
        log_file=log_file_abs,
        verbose=verbose,
    )

    try:
        async for event in runner.run_async(
            user_id="pipeline",
            session_id=sid,
            new_message=types.Content(role="user", parts=[types.Part(text=user_message)]),
        ):
            if not event.content or not event.content.parts:
                continue

            for part in event.content.parts:
                if part.function_call:
                    name = part.function_call.name or ""
                    args = part.function_call.args
                    _log_event(f"  [tool call] {name}({args})", log_file=log_file_abs, verbose=verbose)

                if part.function_response:
                    resp = str(part.function_response.response)
                    preview = resp[:200] + "..." if len(resp) > 200 else resp
                    _log_event(f"  [tool result] {preview}", log_file=log_file_abs, verbose=verbose)

                if part.text and not event.partial:
                    _log_event(f"  [agent] {part.text.strip()}", log_file=log_file_abs, verbose=verbose)
    except Exception as exc:
        _log_event(
            f"Pipeline error encountered: {exc}. Running active container cleanup...",
            log_file=log_file_abs,
            verbose=verbose,
        )
        _process_cleanup()
        # Audit stub: a crashed run must still leave manifest.json +
        # result.json in run_dir so readers see what happened and why.
        # Best-effort only; the original exception is always re-raised.
        try:
            try:
                crashed_session = await session_service.get_session(
                    app_name=app_name, user_id="pipeline", session_id=sid
                )
                stub_state = dict(crashed_session.state)
            except Exception:
                stub_state = dict(initial_state)
            stub_state.pop("run_manifest", None)
            stub_state["result_status"] = TuningStatus.FAIL.value
            stub_state["staging_validated"] = False
            stub_issues = list(stub_state.get("staging_issues") or [])
            crash_note = f"pipeline crash: {exc}"
            if crash_note not in stub_issues:
                stub_issues.append(crash_note)
            stub_state["staging_issues"] = stub_issues
            stub_manifest = _manifest_from_state(stub_state)
            write_manifest(run_dir, stub_manifest)
            stub_state["run_manifest"] = stub_manifest.model_dump()
            _write_output_result(os.path.join(run_dir, "result.json"), stub_state)
        except Exception as stub_exc:
            _log_event(
                f"Crash-stub write warning: {stub_exc}",
                log_file=log_file_abs,
                verbose=verbose,
            )
        raise

    session = await session_service.get_session(
        app_name=app_name, user_id="pipeline", session_id=sid
    )
    final_state = dict(session.state)

    # 3. Persist run-scoped manifest + combined result.
    manifest = _manifest_from_state(final_state)
    write_manifest(run_dir, manifest)
    final_state["run_manifest"] = manifest.model_dump()

    _write_output_result(os.path.join(run_dir, "result.json"), final_state)
    _refresh_latest_pointer(
        results_dir_abs, run_dir, log_file=log_file_abs, verbose=verbose
    )

    _log_event(
        f"Pipeline completed (status={manifest.final_status}). Artifacts written to {run_dir}",
        log_file=log_file_abs,
        verbose=verbose,
    )

    return final_state


def main() -> None:
    """CLI main entry point."""
    parser = build_parser()
    args = parser.parse_args()

    target = os.path.abspath(args.target)
    if not os.path.isdir(target):
        print(f"ERROR: target directory not found: {target}", file=sys.stderr)
        sys.exit(2)

    # Validate the resource contract BEFORE any side effect.
    try:
        _parse_budget(args.cpu_cores, args.memory)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        result = asyncio.run(
            run_pipeline(
                target=target,
                model=args.model,
                db_type=args.db_type,
                cpu_cores_arg=args.cpu_cores,
                memory_arg=args.memory,
                db_config=args.db_config,
                log_file=args.log_file,
                dry_run=args.dry_run,
                verbose=args.verbose,
                db_name=args.db_name,
                buffer_time=args.buffer_time,
                apply_mode=args.apply_mode,
                results_dir=args.results_dir,
                screen_total_rows=args.screen_total_rows,
                screen_max_rows=args.screen_max_rows,
                screening_benchmark=args.screening_benchmark,
                measure_reps=args.measure_reps,
                measure_seconds=args.measure_seconds,
                measure_warmup_seconds=args.measure_warmup_seconds,
                early_stop_min_reps=args.early_stop_min_reps,
                max_set_knobs=args.max_set_knobs,
                max_attempts=args.max_attempts,
                success_candidates=args.success_candidates,
                min_improvement_pct=args.min_improvement_pct,
                max_winners=args.max_winners,
            )
        )
    except Exception as exc:
        _process_cleanup()
        print(f"\n=== Knob Tuner Pipeline FAILED ===\nError: {exc}", file=sys.stderr)
        sys.exit(1)

    final_status = _derive_status(result)
    run_dir = result.get("run_dir") or ""
    manifest = result.get("run_manifest") or {}
    apply_mode = result.get("apply_mode") or args.apply_mode

    print("\n=== Knob Tuner Summary ===")
    print(f"Target:             {target}")
    print(f"Model:              {args.model}")
    print(f"Database Type:      {args.db_type}")
    print(f"Database Name:      {args.db_name}")
    print(f"Apply Mode:         {apply_mode}")
    print(f"Final Status:       {final_status}")
    print(f"Run Directory:      {run_dir}")
    print(f"Log File:           {args.log_file}")

    issues = result.get("staging_issues", []) or []
    if final_status == "FAIL" and issues:
        print(f"\nStaging Validation Issues ({len(issues)}):")
        for idx, issue in enumerate(issues, 1):
            print(f"  {idx}. {issue}")

    live_out = _maybe_parse(result.get("live_result", {}))
    # The manifest mirrors live_result.pending_restart_knobs (shared builder),
    # so the live result is authoritative and the manifest is the fallback.
    restart_knobs = (
        live_out.get("pending_restart_knobs", [])
        or manifest.get("pending_restart_knobs", [])
    )
    manual_sql = live_out.get("manual_sql") or []
    if manual_sql:
        print("\n=== Manual SQL (manual) ===")
        for statement in manual_sql:
            print(f"  {statement}")
        print("Apply these during your maintenance window, then restart the database.")

    print("\n=== Next Steps ===")
    # Phase 1.6: report what actually happened to restart-required knobs —
    # persisted live, skipped under live mode, emitted as manual SQL, or
    # never applied — instead of unconditionally claiming persistence.
    live_status = str(live_out.get("status", "") or "").upper()
    live_reason = str(live_out.get("reason", "") or "")
    applied_knobs = (
        live_out.get("applied_knobs") or result.get("applied_knobs") or []
    )
    persisted_static = live_out.get("persisted_static_knobs") or []
    if restart_knobs:
        knob_names = [
            str(k.get("name") or k.get("knob") or k) if isinstance(k, dict) else str(k)
            for k in restart_knobs
        ]
        knob_names_str = ", ".join(filter(None, knob_names[:3]))
        if persisted_static:
            print("[!] Static configuration parameters have been persisted (e.g. postgresql.auto.conf).")
            print(f"To activate these parameters ({knob_names_str}), restart the database during your next scheduled maintenance window:")
            print("  - Docker:  docker restart <container_name>")
            print("  - Systemd: sudo systemctl restart postgresql (or mysql)")
        elif manual_sql:
            print("[!] Static configuration parameters were NOT applied live; manual SQL was emitted above.")
            print(f"Apply these during your maintenance window, then restart the database to activate ({knob_names_str}):")
            print("  - Docker:  docker restart <container_name>")
            print("  - Systemd: sudo systemctl restart postgresql (or mysql)")
        elif applied_knobs:
            print("[!] Static configuration parameters were skipped under live apply mode (not persisted).")
            print(f"To apply ({knob_names_str}), re-run with --apply-mode manual, then restart the database during your next scheduled maintenance window:")
            print("  - Docker:  docker restart <container_name>")
            print("  - Systemd: sudo systemctl restart postgresql (or mysql)")
        else:
            detail = f"live apply status is {live_status}" if live_status else "live apply did not run"
            if live_reason:
                detail += f": {live_reason}"
            print(f"[!] Static configuration parameters were NOT persisted ({detail}).")
            print("No restart is required (nothing was applied).")
    else:
        print("No database restart is required.")

    try:
        _print_tuning_results(result)
    except Exception:
        pass

    # Phase 1.6: the exit code follows the real status. A failed validation
    # is a failure even in dry-run mode (dry-run only skips mutations).
    if final_status == TuningStatus.FAIL.value:
        sys.exit(1)
    if final_status == TuningStatus.PASS.value or args.dry_run:
        sys.exit(0)
    sys.exit(3)


if __name__ == "__main__":
    main()
