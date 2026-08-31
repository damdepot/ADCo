"""CLI entry point for the ADCo rewriter pipeline.

Usage:
    uv run python -m rewriter <target_dir> [--model MODEL] [--verbose]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid

from dotenv import load_dotenv
from google.genai import types

from src.code_rewriter.agent import create_root_agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

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
    return value if isinstance(value, dict) else {}


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
        "--verbose", "-v",
        action="store_true",
        help="Print detailed progress of each pipeline step",
    )
    parser.add_argument(
        "--output-path",
        default=None,
        metavar="DIR",
        help="Directory to write the output project into (skips sandbox-id sub-dir)",
    )
    args = parser.parse_args()

    target = os.path.abspath(args.target)
    if not os.path.isdir(target):
        print(f"ERROR: target is not a directory: {target}", file=sys.stderr)
        sys.exit(2)

    output_path = os.path.abspath(args.output_path) if args.output_path else None

    try:
        result = asyncio.run(_run_pipeline(target, model=args.model, verbose=args.verbose, output_path=output_path))
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


async def _run_pipeline(target: str, model: str = DEFAULT_MODEL, verbose: bool = False, output_path: str | None = None) -> dict:
    session_service = InMemorySessionService()
    sid = uuid.uuid4().hex[:12]
    app_name = "adco_rewriter"

    await session_service.create_session(
        app_name=app_name,
        user_id="pipeline",
        session_id=sid,
        state={"target": target, "output_path": output_path},
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

            if part.function_response and verbose:
                resp = str(part.function_response.response)
                print(f"  [tool result] {resp[:160]}")

            if part.text and not event.partial:
                if verbose:
                    print(part.text, end="")

    session = await session_service.get_session(app_name=app_name, user_id="pipeline", session_id=sid)
    state = dict(session.state)

    return state


if __name__ == "__main__":
    main()