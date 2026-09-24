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
    ResourceBudget,
    RunManifest,
    SysbenchProfile,
    TuningStatus,
)
from src.knob_tuner.tools.db_connector import load_db_config
from src.knob_tuner.tools.docker_tools import (
    ACTIVE_CONTAINERS,
    cleanup_orphan_containers,
    stop_staging_db,
)
from src.knob_tuner.tools.run_artifacts import (
    application_code_hash,
    create_run_dir,
    new_run_id,
    write_manifest,
)
from src.knob_tuner.tools.knobs import coerce_profile
from src.intent_analyzer.main import run_pipeline as run_intent_analyzer

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

DEFAULT_MODEL = "gemini-3.5-flash-lite"

_cleanup_handlers_registered = False


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
    global _cleanup_handlers_registered
    if not _cleanup_handlers_registered:
        atexit.register(_process_cleanup)
        try:
            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)
        except (ValueError, AttributeError):
            pass
        _cleanup_handlers_registered = True


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


def _load_profile(source: Any) -> SysbenchProfile:
    """Load a :class:`SysbenchProfile` from a path, dict, or profile instance.

    Raises:
        ValueError: If a supplied profile path or payload is missing or invalid.
    """
    if source is None:
        return SysbenchProfile()
    if isinstance(source, SysbenchProfile):
        return source
    if isinstance(source, dict):
        try:
            return SysbenchProfile(**source)
        except Exception as exc:
            raise ValueError(f"invalid sysbench profile: {exc}") from exc
    if isinstance(source, str):
        path = os.path.abspath(source)
        if not os.path.isfile(path):
            raise ValueError(f"sysbench profile file not found: {path}")
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(
                f"invalid sysbench profile JSON at {path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError(f"sysbench profile at {path} must be a JSON object")
        try:
            return SysbenchProfile(**data)
        except Exception as exc:
            raise ValueError(f"invalid sysbench profile at {path}: {exc}") from exc
    raise ValueError(f"unsupported sysbench profile value: {source!r}")


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
        except Exception:
            pass


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
        choices=["none", "dynamic", "persist-static"],
        default="dynamic",
        help=(
            "How validated knobs are applied live: 'dynamic' applies reloadable "
            "knobs; 'persist-static' also persists restart-required knobs for a "
            "manual restart (default: dynamic)"
        ),
    )
    parser.add_argument(
        "--results-dir",
        default="results/dco",
        help="Base directory for run-scoped artifacts (default: results/dco)",
    )
    parser.add_argument(
        "--sysbench-profile",
        default=None,
        help="Optional path to a JSON sysbench profile",
    )
    parser.add_argument(
        "--db-config",
        default="db.config",
        help="Path to database connection config INI file (default: db.config)",
    )
    parser.add_argument(
        "--production-db",
        action="store_true",
        default=False,
        help="Target production environment database instead of staging",
    )
    parser.add_argument(
        "--log-file",
        default="logs/knob_tuner.log",
        help="Path to execution log file (default: logs/knob_tuner.log)",
    )
    parser.add_argument(
        "--knob-path",
        default="out/knob_tuner",
        help="Directory to save generated knob configuration files (default: out/knob_tuner)",
    )
    parser.add_argument(
        "--output-path",
        default=None,
        help="Optional additional path to write the final tuning result",
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
        "--no-cleanup-orphans",
        dest="cleanup_orphans",
        action="store_false",
        default=True,
        help="Skip cleaning up orphan staging database containers on start (default: cleanup enabled)",
    )
    parser.add_argument(
        "--buffer-time",
        type=float,
        default=0.0,
        help="Buffer sleep time in seconds before each LLM call (default: 0.0)",
    )
    return parser


def build_initial_state(
    target: str,
    db_type: str,
    budget: ResourceBudget,
    db_config_path: str,
    production_db: bool,
    log_file: str,
    knob_path: str,
    output_path: str | None,
    dry_run: bool,
    db_name: str = "",
    profile: SysbenchProfile | None = None,
    run_id: str = "",
    run_dir: str = "",
    apply_mode: str = "dynamic",
) -> dict[str, Any]:
    """Construct the initial session state for the knob tuner workflow."""
    profile = profile or SysbenchProfile()
    env = "production" if production_db else "staging"
    state: dict[str, Any] = {
        "target": target,
        "db_type": db_type,
        "db_name": db_name,
        "database": db_name,
        "dbname": db_name,
        "resource_budget": budget.model_dump(),
        "cpu_cores": budget.cpu_cores,
        "memory_gb": budget.memory_gb,
        "memory": budget.memory_gb,
        "sysbench_profile": profile.model_dump(),
        "sysbench_profile_hash": profile.profile_hash(),
        "apply_mode": apply_mode,
        "run_id": run_id,
        "run_dir": run_dir,
        "db_config_path": db_config_path,
        "config_path": db_config_path,
        "production_db": production_db,
        "env": env,
        "log_file": log_file,
        "knob_path": knob_path,
        "output_path": output_path,
        "dry_run": dry_run,
        "validation_attempt_count": 0,
        "max_validation_attempts": 4,
    }

    if os.path.isfile(db_config_path):
        try:
            cfg = load_db_config(db_config_path, db_type=db_type, db_override=db_name)
            state["db_config"] = cfg
            state["database"] = cfg.database
            state["dbname"] = cfg.database
            if production_db:
                state["production_db_config"] = cfg
                state["prod_db_config"] = cfg
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
    """Build a :class:`RunManifest`, preferring one already in state."""
    raw = state.get("run_manifest")
    if isinstance(raw, RunManifest):
        return raw
    if isinstance(raw, dict) and raw:
        try:
            return RunManifest.model_validate(raw)
        except Exception:
            pass

    profile = coerce_profile(state.get("sysbench_profile"))
    status = _status_enum(state)
    target = state.get("target", "") or ""
    attestation = state.get("validation_attestation")
    if isinstance(attestation, dict):
        verified_knobs = attestation.get("verified_knobs", []) or []
    else:
        verified_knobs = []

    attempts = state.get("validation_attempts") or []
    attempt_count = len(attempts) or int(state.get("validation_attempt_count", 0) or 0)

    return RunManifest(
        run_id=state.get("run_id", "") or "",
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        status=status,
        resource_budget=state.get("resource_budget") or {},
        db_engine=state.get("db_type", "") or "",
        db_version=str(state.get("db_version") or ""),
        db_image=state.get("db_image", "") or "",
        application_target=target,
        application_code_hash=application_code_hash(target),
        knob_plan_hash=state.get("knob_plan_hash", "") or "",
        sysbench_profile_hash=state.get("sysbench_profile_hash")
        or profile.profile_hash(),
        seed=profile.seed,
        client_threads=profile.threads,
        attempt_count=attempt_count,
        applied_knobs=state.get("applied_knobs") or [],
        verified_knobs=verified_knobs,
        errors=list(state.get("staging_issues") or []),
        final_status=status.value,
    )


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
        "db_name": state.get("db_name"),
        "resource_budget": state.get("resource_budget"),
        "apply_mode": state.get("apply_mode"),
        "production_db": state.get("production_db", False),
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


async def run_pipeline(
    target: str,
    model: str = DEFAULT_MODEL,
    db_type: str = "postgres",
    cpu_cores_arg: Any = None,
    memory_arg: Any = None,
    db_config: str = "db.config",
    production_db: bool = False,
    log_file: str = "logs/knob_tuner.log",
    knob_path: str = "out/knob_tuner",
    output_path: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    cleanup_orphans: bool = True,
    db_name: str = "",
    buffer_time: float = 0.0,
    apply_mode: str = "dynamic",
    results_dir: str = "results/dco",
    sysbench_profile: Any = None,
    extra_initial_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the knob tuner pipeline using the ADK Runner and session service."""
    register_cleanup_handlers()

    # 1. Resource contract FIRST: fail before any side effect.
    budget = _parse_budget(cpu_cores_arg, memory_arg)
    profile = _load_profile(sysbench_profile)

    target_abs = os.path.abspath(target)
    db_config_abs = os.path.abspath(db_config)
    knob_path_abs = os.path.abspath(knob_path)
    log_file_abs = os.path.abspath(log_file)
    results_dir_abs = os.path.abspath(results_dir)

    os.makedirs(os.path.dirname(log_file_abs), exist_ok=True)
    os.makedirs(knob_path_abs, exist_ok=True)
    os.makedirs(results_dir_abs, exist_ok=True)

    # 2. Run-scoped artifacts.
    run_id = new_run_id()
    run_dir = create_run_dir(results_dir_abs, run_id)

    if cleanup_orphans:
        try:
            orphans = cleanup_orphan_containers()
            if orphans:
                _log_event(
                    f"Cleaned up {len(orphans)} stale orphan container(s): {', '.join(orphans)}",
                    log_file=log_file_abs,
                    verbose=verbose,
                )
        except Exception as exc:
            _log_event(f"Orphan cleanup warning: {exc}", log_file=log_file_abs, verbose=verbose)

    initial_state = build_initial_state(
        target=target_abs,
        db_type=db_type,
        budget=budget,
        db_config_path=db_config_abs,
        production_db=production_db,
        log_file=log_file_abs,
        knob_path=knob_path_abs,
        output_path=os.path.abspath(output_path) if output_path else None,
        dry_run=dry_run,
        db_name=db_name,
        profile=profile,
        run_id=run_id,
        run_dir=run_dir,
        apply_mode=apply_mode,
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

    env_name = "production" if production_db else "staging"
    user_message = (
        f"Tune database configuration knobs for the codebase at: {target_abs}\n\n"
        f"Configuration details:\n"
        f"- Database Type: {db_type}\n"
        f"- Database Name: {db_name}\n"
        f"- CPU Cores: {budget.cpu_cores}\n"
        f"- Memory: {budget.memory_gb} GB\n"
        f"- Target Environment: {env_name}\n"
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
    if output_path:
        _write_output_result(os.path.abspath(output_path), final_state)

    _log_event(
        f"Pipeline completed (status={manifest.final_status}). Artifacts written to {run_dir}",
        log_file=log_file_abs,
        verbose=verbose,
    )

    return final_state


def main() -> None:
    """CLI main entry point."""
    register_cleanup_handlers()
    parser = build_parser()
    args = parser.parse_args()

    target = os.path.abspath(args.target)
    if not os.path.isdir(target):
        print(f"ERROR: target directory not found: {target}", file=sys.stderr)
        sys.exit(2)

    # Validate the resource contract and profile BEFORE any side effect.
    try:
        _parse_budget(args.cpu_cores, args.memory)
        _load_profile(args.sysbench_profile)
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
                production_db=args.production_db,
                log_file=args.log_file,
                knob_path=args.knob_path,
                output_path=args.output_path,
                dry_run=args.dry_run,
                verbose=args.verbose,
                cleanup_orphans=getattr(args, "cleanup_orphans", True),
                db_name=args.db_name,
                buffer_time=getattr(args, "buffer_time", 0.0),
                apply_mode=args.apply_mode,
                results_dir=args.results_dir,
                sysbench_profile=args.sysbench_profile,
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
    restart_knobs = (
        live_out.get("pending_restart_knobs", [])
        or result.get("prod_restart_required_knobs", [])
        or manifest.get("pending_restart_knobs", [])
    )
    print("\n=== Next Steps ===")
    if restart_knobs:
        knob_names = [
            str(k.get("name") or k.get("knob") or k) if isinstance(k, dict) else str(k)
            for k in restart_knobs
        ]
        knob_names_str = ", ".join(filter(None, knob_names[:3]))
        print("[!] Static configuration parameters have been persisted (e.g. postgresql.auto.conf).")
        print(f"To activate these parameters ({knob_names_str}), restart the database during your next scheduled maintenance window:")
        print("  - Docker:  docker restart <container_name>")
        print("  - Systemd: sudo systemctl restart postgresql (or mysql)")
    else:
        print("No database restart is required.")

    if args.dry_run or final_status == TuningStatus.PASS.value:
        sys.exit(0)
    if final_status == TuningStatus.FAIL.value:
        sys.exit(1)
    sys.exit(3)


if __name__ == "__main__":
    main()
