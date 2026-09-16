"""Intent analyzer runner and CLI entry point."""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import os
import sys
import uuid
from typing import Any

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from src.intent_analyzer.agent import create_intent_analyzer_agent

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

DEFAULT_MODEL = "gemini-3.5-flash-lite"


def _maybe_parse(val: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            pass
    return val


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ADCo Codebase Intent Analyzer — extract DB patterns & workload characteristics"
    )
    p.add_argument("target", help="Path to the target codebase")
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini model (default: {DEFAULT_MODEL})",
    )
    p.add_argument(
        "--log-file",
        default="logs/intent_analyzer.log",
        help="Path to execution log file",
    )
    p.add_argument(
        "--output-path",
        default="out/intent_analyzer/result.json",
        help="Path to write final intent analysis output",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Print verbose progress")
    return p


async def run_pipeline(
    target: str,
    model: str = DEFAULT_MODEL,
    log_file: str = "logs/intent_analyzer.log",
    output_path: str = "out/intent_analyzer/result.json",
    verbose: bool = False,
    extra_initial_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute intent analyzer pipeline on the given target codebase."""
    target_abs = os.path.abspath(target)
    if not os.path.isdir(target_abs):
        raise ValueError(f"Target directory does not exist: {target_abs}")

    os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    initial_state = {
        "target": target_abs,
        "log_file": os.path.abspath(log_file),
        "output_path": os.path.abspath(output_path),
    }
    if extra_initial_state:
        initial_state.update(extra_initial_state)

    session_service = InMemorySessionService()
    sid = uuid.uuid4().hex[:12]
    app_name = "adco_intent_analyzer"

    await session_service.create_session(
        app_name=app_name,
        user_id="pipeline",
        session_id=sid,
        state=initial_state,
    )

    agent = create_intent_analyzer_agent(model)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)

    msg = (
        f"Analyze the codebase at: {target_abs}\n"
        "1. Scan the codebase files.\n"
        "2. Select database-relevant files.\n"
        "3. Extract code optimization targets and structured workload characteristics."
    )

    user_content = types.Content(
        role="user",
        parts=[types.Part.from_text(text=msg)],
    )

    async for event in runner.run_async(
        user_id="pipeline",
        session_id=sid,
        new_message=user_content,
    ):
        if verbose and hasattr(event, "content"):
            print(f"[intent_analyzer] {event.content}")

    session = await session_service.get_session(
        app_name=app_name,
        user_id="pipeline",
        session_id=sid,
    )
    final_state = dict(session.state) if session else {}

    # Extract intent results
    intent_raw = final_state.get("intent_extractor_output") or {}
    intent_parsed = _maybe_parse(intent_raw)

    # If the orchestrator didn't invoke intent_extractor or it produced no output, run it directly
    if not intent_parsed or not isinstance(intent_parsed, dict) or not intent_parsed.get("optimization_targets"):
        if "file_selector_output" in final_state:
            from src.intent_analyzer.sub_agents.intent_extractor.agent import create_intent_extractor_agent
            ie_agent = create_intent_extractor_agent(model)
            ie_runner = Runner(agent=ie_agent, app_name=app_name, session_service=session_service)
            ie_msg = types.Content(
                role="user",
                parts=[types.Part.from_text(text="Extract database interaction patterns, optimization targets, and workload characteristics from the selected files.")],
            )
            async for event in ie_runner.run_async(
                user_id="pipeline",
                session_id=sid,
                new_message=ie_msg,
            ):
                if verbose and hasattr(event, "content"):
                    print(f"[intent_extractor_fallback] {event.content}")
            session = await session_service.get_session(
                app_name=app_name,
                user_id="pipeline",
                session_id=sid,
            )
            final_state = dict(session.state) if session else {}
            intent_raw = final_state.get("intent_extractor_output") or {}
            intent_parsed = _maybe_parse(intent_raw)

    if not intent_parsed or not isinstance(intent_parsed, dict):
        raise RuntimeError(
            f"Intent extractor produced no structured output for target: {target_abs}. "
            "Ensure the target codebase contains database interaction code and is accessible."
        )
    final_state["intent_output"] = intent_parsed

    if isinstance(intent_parsed, dict) and "workload" in intent_parsed:
        final_state["workload_info"] = intent_parsed["workload"]

    from src.intent_analyzer.models import IntentAnalyzerResult
    result_obj = IntentAnalyzerResult(
        timestamp=datetime.datetime.now().isoformat(),
        target=target_abs,
        model=model,
        intent_output=intent_parsed,
    )

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            result_obj.model_dump(),
            f,
            indent=2,
            default=str,
        )

    return final_state


def main() -> None:
    p = build_parser()
    args = p.parse_args()

    target = os.path.abspath(args.target)
    if not os.path.isdir(target):
        print(f"ERROR: target is not a directory: {target}", file=sys.stderr)
        sys.exit(2)

    try:
        asyncio.run(
            run_pipeline(
                target=target,
                model=args.model,
                log_file=args.log_file,
                output_path=args.output_path,
                verbose=args.verbose,
            )
        )
    except Exception as exc:
        print(f"\n=== Intent Analyzer FAILED ===\nError: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\n=== Intent Analyzer COMPLETED ===")
    sys.exit(0)
