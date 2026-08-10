"""CLI entry point for the ADCo rewriter pipeline.

Usage:
    uv run python -m rewriter <target_dir> [--model MODEL] [--verbose]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid

from dotenv import load_dotenv
from google.genai import types

from rewriter.agent import create_root_agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from telemetry import RewriterRun, extract_tokens

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

DEFAULT_MODEL = "gemini-2.5-flash"

_STEP_NAMES = {
    "scan_codebase", "file_selector", "intent_extractor",
    "copy_to_sandbox", "get_optimization_strategies",
    "code_optimizer", "verifier",
}

_RETRYABLE_STEPS = {"code_optimizer", "verifier"}

_STEP_OUTPUT_KEYS = {
    "scan_codebase": "scan_result",
    "file_selector": "file_selector_output",
    "intent_extractor": "intent_extractor_output",
    "copy_to_sandbox": "sandbox",
    "get_optimization_strategies": "strategies",
    "code_optimizer": "code_optimizer_output",
    "verifier": "verifier_output",
}


def main() -> None:
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
        "--json",
        action="store_true",
        help="Output result as JSON",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed progress of each pipeline step",
    )
    args = parser.parse_args()

    target = os.path.abspath(args.target)
    if not os.path.isdir(target):
        print(f"ERROR: target is not a directory: {target}", file=sys.stderr)
        sys.exit(2)

    try:
        result = asyncio.run(_run_pipeline(target, model=args.model, verbose=args.verbose))
    except Exception as exc:
        print(f"\n=== Pipeline FAILED ===\nError: {exc}", file=sys.stderr)
        sys.exit(1)

    verdict = result.get("verifier_output", {})
    status = verdict.get("status", "FAIL") if isinstance(verdict, dict) else "FAIL"
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


async def _run_pipeline(target: str, model: str = DEFAULT_MODEL, verbose: bool = False) -> dict:
    session_service = InMemorySessionService()
    sid = uuid.uuid4().hex[:12]
    app_name = "adco_rewriter"

    await session_service.create_session(
        app_name=app_name,
        user_id="pipeline",
        session_id=sid,
        state={"target": target},
    )

    agent = create_root_agent(model)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)

    user_message = (
        f"Optimize the database interaction code in the codebase at: {target}\n\n"
        f"Follow the pipeline order from your instructions: scan the codebase, "
        f"delegate to file_selector (pass it the file listing), delegate to "
        f"intent_extractor, copy_to_sandbox, get_optimization_strategies, "
        f"delegate to code_optimizer, then delegate to verifier. Report the "
        f"verifier's verdict when done."
    )

    with RewriterRun(
        run_type="rewriter",
        model_name=model,
        target=target,
    ) as telemetry:

        current_step: str = ""
        step_start: float = 0.0
        step_input_tokens: int = 0
        step_output_tokens: int = 0
        retry_counts: dict[str, int] = {}

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
                    if verbose:
                        print(f"  [tool] {name}({part.function_call.args})")

                    if name in _STEP_NAMES:
                        _maybe_record_step(
                            telemetry, current_step, step_start,
                            step_input_tokens, step_output_tokens,
                        )
                        if name in _RETRYABLE_STEPS:
                            retry_counts[name] = retry_counts.get(name, 0) + 1
                            current_step = f"{name}_attempt_{retry_counts[name]}" if retry_counts[name] > 1 else name
                        else:
                            current_step = name
                        step_start = time.time()
                        step_input_tokens = 0
                        step_output_tokens = 0

                if part.function_response and verbose:
                    resp = str(part.function_response.response)
                    print(f"  [tool result] {resp[:160]}")

                if part.text and not event.partial:
                    if verbose:
                        print(part.text, end="")

            if hasattr(event, "usage_metadata") and event.usage_metadata:
                it, ot = extract_tokens(event.usage_metadata)
                step_input_tokens += it
                step_output_tokens += ot

        _maybe_record_step(
            telemetry, current_step, step_start,
            step_input_tokens, step_output_tokens,
        )

        session = await session_service.get_session(app_name=app_name, user_id="pipeline", session_id=sid)
        state = dict(session.state)
        sandbox = state.get("sandbox", "")
        if sandbox:
            telemetry.set_sandbox_id(os.path.basename(sandbox))

        for step_name, state_key in _STEP_OUTPUT_KEYS.items():
            output = state.get(state_key)
            if output is not None:
                telemetry.update_step_output(step_name, output)

    return state


def _maybe_record_step(
    telemetry: RewriterRun,
    name: str,
    start_time: float,
    input_tokens: int,
    output_tokens: int,
) -> None:
    if not name or not start_time:
        return
    duration_ms = int((time.time() - start_time) * 1000)
    telemetry.record_step(
        step=name,
        duration_ms=duration_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


if __name__ == "__main__":
    main()