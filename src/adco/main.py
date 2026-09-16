"""Unified ADCo pipeline: intent_analyzer → code_rewriter → knob_tuner."""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
from typing import Any

from dotenv import load_dotenv

from src.adco.auth_check import check_auth
from src.intent_analyzer.main import run_pipeline as intent_analyzer_pipeline
from src.code_rewriter.main import run_pipeline as rewriter_pipeline, _maybe_parse
from src.knob_tuner.main import run_pipeline as tuner_pipeline

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

DEFAULT_MODEL = "gemini-3.5-flash-lite"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ADCo unified pipeline — intent analyzer + code rewriter + knob tuner"
    )
    # Target
    p.add_argument("target", help="Path to the codebase to optimize")
    # Shared
    p.add_argument(
        "--mode",
        choices=["all", "rewrite-only", "tune-only"],
        default="all",
        help="Operational mode: 'all' (intent + rewrite + tune), 'rewrite-only' (intent + rewrite), or 'tune-only' (intent + tune). Default: all",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini model (default: {DEFAULT_MODEL})",
    )
    p.add_argument("--log-file", default="logs/adco.log", help="Path to execution log file")
    p.add_argument(
        "--output-path",
        default="out/adco/result.json",
        help="Path to write final combined result output",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Print detailed progress")
    # Intent Analyzer
    p.add_argument(
        "--intent-output",
        default="out/adco/intent_result.json",
        help="Path to write intent analyzer output",
    )
    # Rewriter
    p.add_argument(
        "--rewriter-output",
        default="out/adco/rewriter_result.json",
        help="Path to write rewriter output",
    )
    p.add_argument(
        "--sandbox-dir",
        default=None,
        help="Directory to write the rewritten project into (rewrite modes only)",
    )
    # Knob tuner
    p.add_argument("--db-name", default="", help="Database name (required for 'all' or 'tune-only' mode)")
    p.add_argument(
        "--db-type",
        choices=["postgres", "mysql"],
        default="postgres",
        help="Database engine type (postgres or mysql)",
    )
    p.add_argument("--db-config", default="db.config", help="Path to database config INI file")
    p.add_argument("--cpu-cores", default="auto", help="Number of CPU cores allocated for DB")
    p.add_argument("--memory", default="auto", help="Database memory limit in GB")
    p.add_argument(
        "--knob-path",
        default="out/adco/knobs",
        help="Directory to save generated knob configuration files",
    )
    p.add_argument(
        "--tuner-output",
        default="out/adco/knob_result.json",
        help="Path to write knob tuner result output",
    )
    p.add_argument(
        "--production-db",
        action="store_true",
        default=False,
        help="Target production environment database instead of staging",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate tuning process without applying modifications to live database",
    )
    return p


async def run_pipeline(
    target: str,
    model: str = DEFAULT_MODEL,
    mode: str = "all",
    log_file: str = "logs/adco.log",
    output_path: str = "out/adco/result.json",
    intent_output_path: str = "out/adco/intent_result.json",
    rewriter_output: str = "out/adco/rewriter_result.json",
    sandbox_dir: str | None = None,
    db_name: str = "",
    db_type: str = "postgres",
    db_config: str = "db.config",
    cpu_cores_arg: Any = "auto",
    memory_arg: Any = "auto",
    knob_path: str = "out/adco/knobs",
    tuner_output: str = "out/adco/knob_result.json",
    production_db: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    target_abs = os.path.abspath(target)

    # ── Phase 1: Codebase Intent Analyzer ──────────────────
    intent_state = await intent_analyzer_pipeline(
        target=target_abs,
        model=model,
        log_file=log_file,
        output_path=intent_output_path,
        verbose=verbose,
    )

    intent_output = intent_state.get("intent_output") or intent_state.get("intent_extractor_output") or {}
    workload_info = intent_state.get("workload_info") or (intent_output.get("workload") if isinstance(intent_output, dict) else {})

    # ── Phase 2: Code Rewriter ─────────────────────────────
    rewriter_state = {}
    sandbox = target_abs
    if mode in ("all", "rewrite-only"):
        if not intent_output or not isinstance(intent_output, dict) or not intent_output.get("optimization_targets"):
            raise RuntimeError(
                "Intent analyzer returned no output or no optimization_targets. "
                "The pipeline cannot proceed to code rewriting without intent context. "
                f"Target: {target}"
            )
        rewriter_extra_state: dict[str, Any] = {
            "intent_output": intent_output,
            "intent_extractor_output": intent_output,
        }

        rewriter_state = await rewriter_pipeline(
            target=target_abs,
            model=model,
            log_file=log_file,
            output_path=rewriter_output,
            sandbox_dir=sandbox_dir,
            verbose=verbose,
            extra_initial_state=rewriter_extra_state,
        )

        verdict = _maybe_parse(rewriter_state.get("verifier_output", {}))
        if verdict.get("status") != "PASS":
            raise RuntimeError(
                f"Code rewriter FAILED — pipeline stopped. "
                f"Category: {verdict.get('category')}, "
                f"Reason: {verdict.get('reason')}"
            )

        sandbox = rewriter_state.get("sandbox") or target_abs
    else:
        if verbose:
            print("Skipping code rewriter phase as mode is 'tune-only'.")

    # ── Phase 3: Knob Tuner ────────────────────────────────
    tuner_state = {}
    if mode in ("all", "tune-only"):
        tuner_extra_state: dict[str, Any] = {}
        if workload_info:
            tuner_extra_state["workload_info"] = workload_info

        tuner_state = await tuner_pipeline(
            target=sandbox,
            model=model,
            db_type=db_type,
            cpu_cores_arg=cpu_cores_arg,
            memory_arg=memory_arg,
            db_config=db_config,
            production_db=production_db,
            log_file=log_file,
            knob_path=knob_path,
            output_path=tuner_output,
            dry_run=dry_run,
            verbose=verbose,
            db_name=db_name,
            extra_initial_state=tuner_extra_state,
        )
    else:
        if verbose:
            print("Skipping knob tuning phase as mode is 'rewrite-only'.")

    # ── Combined output ────────────────────────────────────
    combined: dict[str, Any] = {
        "timestamp": datetime.datetime.now().isoformat(),
        "mode": mode,
        "target": target_abs,
        "sandbox": sandbox,
        "model": model,
        "intent_analyzer": _maybe_parse(
            open(intent_output_path).read() if os.path.exists(intent_output_path) else "{}"
        ),
        "rewriter": _maybe_parse(
            open(rewriter_output).read() if os.path.exists(rewriter_output) else "{}"
        ),
        "knob_tuner": _maybe_parse(
            open(tuner_output).read() if os.path.exists(tuner_output) else "{}"
        ),
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=2, default=str)

    return combined


def main() -> None:
    p = build_parser()
    args = p.parse_args()

    target = os.path.abspath(args.target)
    if not os.path.isdir(target):
        print(f"ERROR: target is not a directory: {target}", file=sys.stderr)
        sys.exit(2)

    mode = args.mode

    if mode in ("all", "tune-only") and not args.db_name:
        print("ERROR: --db-name is required when mode is 'all' or 'tune-only'", file=sys.stderr)
        sys.exit(2)

    check_auth()

    try:
        asyncio.run(
            run_pipeline(
                target=target,
                model=args.model,
                mode=mode,
                log_file=args.log_file,
                output_path=args.output_path,
                intent_output_path=args.intent_output,
                rewriter_output=args.rewriter_output,
                sandbox_dir=args.sandbox_dir,
                db_name=args.db_name,
                db_type=args.db_type,
                db_config=args.db_config,
                cpu_cores_arg=args.cpu_cores,
                memory_arg=args.memory,
                knob_path=args.knob_path,
                tuner_output=args.tuner_output,
                production_db=args.production_db,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
        )
    except Exception as exc:
        print(f"\n=== ADCo Pipeline FAILED ===\nError: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\n=== ADCo Pipeline COMPLETED ===")
    sys.exit(0)
