"""CLI entry point for the ADCo rewriter pipeline.

Usage:
    uv run python -m src.code_rewriter <target_dir> [--model MODEL] [--verbose]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
import uuid
from typing import Any

from dotenv import load_dotenv
from google.genai import types

from google.adk.models import Gemini
from src.code_rewriter._common import _maybe_parse
from src.code_rewriter.agent import create_root_agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from src.intent_analyzer.main import run_pipeline as run_intent_analyzer
from src.code_rewriter.tools.pipeline_analysis import build_contracts_from_intent, format_dependency_slice_map

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

DEFAULT_MODEL = "gemini-3.5-flash-lite"


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
    parser = argparse.ArgumentParser(
        description="ADCo Rewriter — application-database co-optimization pipeline",
    )
    parser.add_argument(
        "target",
        help="Path to the codebase to optimize",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini model to use for all agents (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--log-file",
        default="logs/code_rewriter.log",
        help="Path to execution log file (default: logs/code_rewriter.log)",
    )
    parser.add_argument(
        "--output-path",
        default="out/code_rewriter/result.json",
        help="Path to write final tuning result output (default: out/code_rewriter/result.json)",
    )
    parser.add_argument(
        "--sandbox-dir",
        default=None,
        help="Directory to write the output project into (skips sandbox-id sub-dir)",
    )
    parser.add_argument(
        "--buffer-time",
        type=float,
        default=0.0,
        help="Buffer sleep time in seconds before each LLM call (default: 0.0)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed progress of each pipeline step",
    )
    return parser


def _write_output_result(output_path: str, state: dict[str, Any], model: str = "") -> None:
    """Serialize and write final tuning outcome to the output path."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    result_data = {
        "timestamp": datetime.datetime.now().isoformat(),
        "target": state.get("target"),
        "model": model,
        "sandbox": state.get("sandbox"),
        "status": _maybe_parse(state.get("verifier_output")).get("status", "FAIL"),
        "modified_files": state.get("modified_files", []),
        "outputs": {
            "scan_result": _maybe_parse(state.get("scan_result", state.get("intent_output", {}))),
            "file_selector_output": _maybe_parse(state.get("file_selector_output", {})),
            "intent_output": _maybe_parse(state.get("intent_output")),
            "intent_extractor_output": _maybe_parse(state.get("intent_extractor_output")),
            "code_optimizer_output": _maybe_parse(state.get("code_optimizer_output")),
            "verifier_output": _maybe_parse(state.get("verifier_output")),
            "deterministic_verification": _maybe_parse(state.get("deterministic_verification", {})),
        }
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result_data, f, indent=2, default=str)


async def run_pipeline(
    target: str,
    model: str = DEFAULT_MODEL,
    log_file: str = "logs/code_rewriter.log",
    output_path: str = "out/code_rewriter/result.json",
    sandbox_dir: str | None = None,
    verbose: bool = False,
    extra_initial_state: dict[str, Any] | None = None,
    buffer_time: float = 0.0,
) -> dict[str, Any]:
    target_abs = os.path.abspath(target)
    log_file_abs = os.path.abspath(log_file)
    output_path_abs = os.path.abspath(output_path)
    sandbox_dir_abs = os.path.abspath(sandbox_dir) if sandbox_dir else None
    
    os.makedirs(os.path.dirname(log_file_abs), exist_ok=True)
    os.makedirs(os.path.dirname(output_path_abs), exist_ok=True)

    initial_state = {
        "target": target_abs,
        "sandbox_dir": sandbox_dir_abs,
        "attempt_count": 0,
    }
    if extra_initial_state:
        initial_state.update(extra_initial_state)

    # Standalone fallback: If intent has not been extracted, run intent_analyzer first
    if "intent_extractor_output" not in initial_state and "intent_output" not in initial_state:
        _log_event(
            f"Intent not in state; running intent_analyzer on {target_abs}",
            log_file=log_file_abs,
            verbose=verbose,
        )
        intent_state = await run_intent_analyzer(
            target=target_abs,
            model=model,
            log_file=log_file_abs,
            verbose=verbose,
        )
        intent = intent_state.get("intent_output") or intent_state.get("intent_extractor_output")
        if intent:
            initial_state["intent_output"] = intent
            initial_state["intent_extractor_output"] = intent

    intent_output = initial_state.get("intent_output")
    if intent_output:
        _log_event(f"Building rewrite contracts from intent...", log_file=log_file_abs, verbose=verbose)
        try:
            analyses, contracts = build_contracts_from_intent(target_abs, intent_output)
            initial_state["rewrite_contracts"] = [c.model_dump() for c in contracts]
            _log_event(f"Built {len(contracts)} rewrite contracts.", log_file=log_file_abs, verbose=verbose)
            
            slice_map = format_dependency_slice_map(analyses, contracts, target_abs)
            initial_state["pipeline_analysis_markdown"] = slice_map
        except Exception as e:
            _log_event(f"Error building contracts: {e}", log_file=log_file_abs, verbose=verbose)

    session_service = InMemorySessionService()
    sid = uuid.uuid4().hex[:12]
    app_name = "adco_rewriter"

    await session_service.create_session(
        app_name=app_name,
        user_id="pipeline",
        session_id=sid,
        state=initial_state,
    )

    if isinstance(model, str):
        resilient_model = Gemini(model=model, retry_options=types.HttpRetryOptions(initial_delay=1, attempts=5, exp_base=2))
    else:
        resilient_model = model

    agent = create_root_agent(resilient_model, buffer_time=buffer_time)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)

    user_message = (
        f"Optimize the database interaction code in the codebase at: {target_abs}\n\n"
        f"The database interaction intent has already been extracted into session state. "
        f"Follow the pipeline order: copy_to_sandbox, get_optimization_strategies, "
        f"delegate to code_optimizer, then delegate to verifier. Report the "
        f"verifier's verdict when done."
    )
    
    _log_event(
        f"Starting code_rewriter pipeline (model={model}, target={target_abs})",
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

    session = await session_service.get_session(app_name=app_name, user_id="pipeline", session_id=sid)
    state = dict(session.state)

    _write_output_result(output_path_abs, state, model=model)
    _log_event(f"Pipeline completed. Output written to {output_path_abs}", log_file=log_file_abs, verbose=verbose)

    return state

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    target = os.path.abspath(args.target)
    if not os.path.isdir(target):
        print(f"ERROR: target is not a directory: {target}", file=sys.stderr)
        sys.exit(2)

    try:
        result = asyncio.run(run_pipeline(
            target=target,
            model=args.model,
            log_file=args.log_file,
            output_path=args.output_path,
            sandbox_dir=args.sandbox_dir,
            verbose=args.verbose,
            buffer_time=args.buffer_time,
        ))
    except Exception as exc:
        print(f"\n=== Pipeline FAILED ===\nError: {exc}", file=sys.stderr)
        sys.exit(1)

    verdict = _maybe_parse(result.get("verifier_output", {}))
    status = verdict.get("status", "FAIL")
    sandbox = result.get("sandbox", "")
    modified = result.get("modified_files", [])

    print(f"\n=== Pipeline {'PASSED' if status == 'PASS' else 'FAILED'} ===")
    print(f"Target:    {target}")
    print(f"Model:     {args.model}")
    print(f"Sandbox:   {sandbox}")
    print(f"Modified:  {len(modified)} file(s)")
    if status == "FAIL":
        print(f"Category:  {verdict.get('category', 'N/A')}")
        print(f"Reason:    {verdict.get('reason', 'N/A')}")
        print(f"Detail:    {verdict.get('detail', 'N/A')}")
    if args.verbose and verdict:
        print(f"Verdict:   {verdict}")
        
    sys.exit(0 if status == "PASS" else 1)


if __name__ == "__main__":
    main()