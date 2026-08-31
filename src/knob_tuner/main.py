"""CLI entry point for the ADCo knob_tuner pipeline.

Usage:
    uv run python -m src.knob_tuner <target_dir> [OPTIONS]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import re
import sys
import uuid
from typing import Any

from dotenv import load_dotenv
from google.genai import types

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from src.knob_tuner.agent import create_root_agent
from src.knob_tuner.tools.db_connector import DBConfig, load_db_config

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

DEFAULT_MODEL = "gemini-3.5-flash-lite"


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


def _parse_cpu_cores(val: Any) -> int:
    """Parse CPU cores count from input argument, supporting 'auto'."""
    if val is None or str(val).strip().lower() == "auto":
        return os.cpu_count() or 1
    try:
        cores = int(val)
        if cores <= 0:
            raise ValueError()
        return cores
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid --cpu-cores value: '{val}'. Must be a positive integer or 'auto'."
        )


def _parse_memory(val: Any) -> float:
    """Parse memory limit in GB from input argument, supporting 'auto'."""
    if val is None or str(val).strip().lower() == "auto":
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return round((pages * page_size) / (1024**3), 2)
        except (AttributeError, ValueError, OSError):
            return 1.0
    try:
        mem = float(val)
        if mem <= 0:
            raise ValueError()
        return mem
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid --memory value: '{val}'. Must be a positive float or 'auto'."
        )


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
        help=f"Gemini model to use for orchestrator and sub-agents (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--db-type",
        choices=["postgres", "mysql"],
        default="postgres",
        help="Target database engine type (postgres or mysql, default: postgres)",
    )
    parser.add_argument(
        "--cpu-cores",
        default="auto",
        help="Number of CPU cores allocated for database (default: auto)",
    )
    parser.add_argument(
        "--memory",
        default="auto",
        help="Database memory limit in GB (default: auto)",
    )
    parser.add_argument(
        "--db-config",
        default="db.config",
        help="Path to database configuration INI file (default: db.config)",
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
        default="out/knob_tuner/result.json",
        help="Path to write final tuning result output (default: out/knob_tuner/result.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate tuning process without applying modifications to live database",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Print detailed execution progress and sub-agent events",
    )
    return parser


def build_initial_state(
    target: str,
    db_type: str,
    cpu_cores: int,
    memory_gb: float,
    db_config_path: str,
    production_db: bool,
    log_file: str,
    knob_path: str,
    output_path: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Construct initial state dictionary for the knob tuner session."""
    env = "production" if production_db else "staging"
    state: dict[str, Any] = {
        "target": target,
        "db_type": db_type,
        "cpu_cores": cpu_cores,
        "memory_gb": memory_gb,
        "memory": memory_gb,
        "db_config_path": db_config_path,
        "config_path": db_config_path,
        "production_db": production_db,
        "env": env,
        "log_file": log_file,
        "knob_path": knob_path,
        "output_path": output_path,
        "dry_run": dry_run,
        "retry_count": 0,
    }

    if os.path.isfile(db_config_path):
        try:
            stg_cfg = load_db_config(db_config_path, env="staging", db_type=db_type)
            state["staging_db_config"] = stg_cfg
        except Exception:
            pass

        try:
            prod_cfg = load_db_config(db_config_path, env="production", db_type=db_type)
            state["production_db_config"] = prod_cfg
            state["prod_db_config"] = prod_cfg
        except Exception:
            pass

        try:
            curr_cfg = load_db_config(db_config_path, env=env, db_type=db_type)
            state["db_config"] = curr_cfg
        except Exception:
            pass

    return state


def _write_output_result(output_path: str, state: dict[str, Any]) -> None:
    """Serialize and write final tuning outcome to the output path."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    result_data = {
        "timestamp": datetime.datetime.now().isoformat(),
        "target": state.get("target"),
        "db_type": state.get("db_type"),
        "cpu_cores": state.get("cpu_cores"),
        "memory_gb": state.get("memory_gb"),
        "production_db": state.get("production_db", False),
        "dry_run": state.get("dry_run", False),
        "staging_validated": state.get("staging_validated", False),
        "intent_analyzer_output": _maybe_parse(state.get("intent_analyzer_output")),
        "knob_recommender_output": _maybe_parse(state.get("knob_recommender_output")),
        "knob_checker_output": _maybe_parse(state.get("knob_checker_output")),
        "live_tuner_output": _maybe_parse(state.get("live_tuner_output")),
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result_data, f, indent=2, default=str)


async def run_pipeline(
    target: str,
    model: str = DEFAULT_MODEL,
    db_type: str = "postgres",
    cpu_cores_arg: Any = "auto",
    memory_arg: Any = "auto",
    db_config: str = "db.config",
    production_db: bool = False,
    log_file: str = "logs/knob_tuner.log",
    knob_path: str = "out/knob_tuner",
    output_path: str = "out/knob_tuner/result.json",
    dry_run: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Execute the knob tuner pipeline using Google ADK Runner and session service."""
    target_abs = os.path.abspath(target)
    db_config_abs = os.path.abspath(db_config)
    knob_path_abs = os.path.abspath(knob_path)
    output_path_abs = os.path.abspath(output_path)
    log_file_abs = os.path.abspath(log_file)

    os.makedirs(os.path.dirname(log_file_abs), exist_ok=True)
    os.makedirs(knob_path_abs, exist_ok=True)
    os.makedirs(os.path.dirname(output_path_abs), exist_ok=True)

    cpu_cores = _parse_cpu_cores(cpu_cores_arg)
    memory_gb = _parse_memory(memory_arg)

    session_service = InMemorySessionService()
    sid = uuid.uuid4().hex[:12]
    app_name = "knob_tuner"

    initial_state = build_initial_state(
        target=target_abs,
        db_type=db_type,
        cpu_cores=cpu_cores,
        memory_gb=memory_gb,
        db_config_path=db_config_abs,
        production_db=production_db,
        log_file=log_file_abs,
        knob_path=knob_path_abs,
        output_path=output_path_abs,
        dry_run=dry_run,
    )

    await session_service.create_session(
        app_name=app_name,
        user_id="pipeline",
        session_id=sid,
        state=initial_state,
    )

    agent = create_root_agent(model=model)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)

    env_name = "production" if production_db else "staging"
    user_message = (
        f"Tune database configuration knobs for the codebase at: {target_abs}\n\n"
        f"Configuration details:\n"
        f"- Database Type: {db_type}\n"
        f"- CPU Cores: {cpu_cores}\n"
        f"- Memory: {memory_gb} GB\n"
        f"- Target Environment: {env_name}\n"
        f"- Database Config: {db_config_abs}\n"
        f"- Dry Run: {dry_run}\n"
        f"- Knob Path: {knob_path_abs}\n\n"
        f"Execute the pipeline in order:\n"
        f"1. Delegate to intent_analyzer to extract schema, current knobs, hardware, and workload patterns.\n"
        f"2. Delegate to knob_recommender to formulate tuned recommendations.\n"
        f"3. Delegate to knob_checker to validate recommendations in staging (loop up to 3 retries / 4 total attempts if FAIL).\n"
        f"4. If knob_checker passes, delegate to live_tuner to apply dynamic knobs to production."
    )

    _log_event(
        f"Starting knob_tuner pipeline (model={model}, db_type={db_type}, cores={cpu_cores}, mem={memory_gb}GB, dry_run={dry_run})",
        log_file=log_file_abs,
        verbose=verbose,
    )

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

    session = await session_service.get_session(
        app_name=app_name, user_id="pipeline", session_id=sid
    )
    final_state = dict(session.state)

    _write_output_result(output_path_abs, final_state)
    _log_event(f"Pipeline completed. Output written to {output_path_abs}", log_file=log_file_abs, verbose=verbose)

    return final_state


def main() -> None:
    """CLI main entry point."""
    parser = build_parser()
    args = parser.parse_args()

    target = os.path.abspath(args.target)
    if not os.path.isdir(target):
        print(f"ERROR: target directory not found: {target}", file=sys.stderr)
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
            )
        )
    except Exception as exc:
        print(f"\n=== Knob Tuner Pipeline FAILED ===\nError: {exc}", file=sys.stderr)
        sys.exit(1)

    checker_output = _maybe_parse(result.get("knob_checker_output", {}))
    live_output = _maybe_parse(result.get("live_tuner_output", {}))
    checker_status = checker_output.get("status", "UNKNOWN")
    live_status = live_output.get("status", "SKIPPED" if checker_status != "PASS" else "COMPLETED")

    print("\n=== Knob Tuner Summary ===")
    print(f"Target:             {target}")
    print(f"Model:              {args.model}")
    print(f"Database Type:      {args.db_type}")
    print(f"Staging Validation: {checker_status}")
    print(f"Live Tuning Status: {live_status}")
    print(f"Output File:        {args.output_path}")
    print(f"Log File:           {args.log_file}")

    if checker_status == "FAIL":
        issues = checker_output.get("issues", [])
        print(f"\nStaging Validation Issues ({len(issues)}):")
        for idx, issue in enumerate(issues, 1):
            knob = issue.get("knob", "N/A")
            desc = issue.get("description", "N/A")
            sugg = issue.get("suggestion", "")
            print(f"  {idx}. [{knob}] {desc}")
            if sugg:
                print(f"     Suggestion: {sugg}")

    is_success = (checker_status == "PASS" or args.dry_run)
    sys.exit(0 if is_success else 1)


if __name__ == "__main__":
    main()
