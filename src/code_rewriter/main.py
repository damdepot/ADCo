"""Programmatic entry point for the ADCo rewriter pipeline.

Driven by the unified pipeline (``src.adco``) via :func:`run_pipeline`; the
intent analyzer always runs first, so the rewriter has no standalone CLI.
"""

from __future__ import annotations

import datetime
import json
import os
import uuid
from typing import Any

from dotenv import load_dotenv
from google.genai import types

from google.adk.models import Gemini
from src.code_rewriter._common import _maybe_parse
from src.code_rewriter.agent import create_root_agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from src.code_rewriter.tools.pipeline_analysis import build_contracts_from_intent, build_target_context_map
from src.code_rewriter.tools.db_interaction import build_read_write_map

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


def _write_output_result(output_path: str, state: dict[str, Any], model: str = "") -> None:
    """Serialize and write the rewrite outcome to the output path."""
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
            "optimizer_output": _maybe_parse(state.get("optimizer_output")),
            "verifier_output": _maybe_parse(state.get("verifier_output")),
            "deterministic_verification": _maybe_parse(state.get("deterministic_verification", {})),
            "transformation_risk": _maybe_parse(state.get("transformation_risk", [])),
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

    # Intent is a hard requirement: the unified pipeline (src.adco) always runs
    # the intent analyzer first and passes its output via extra_initial_state.
    intent_output = initial_state.get("intent_output") or initial_state.get("intent_extractor_output")
    if not intent_output:
        raise RuntimeError(
            "intent_output is required — run the unified pipeline (src.adco) so the "
            "intent analyzer runs first, or pass intent_output in extra_initial_state."
        )

    _log_event("Building rewrite contracts from intent...", log_file=log_file_abs, verbose=verbose)
    try:
        analyses, contracts = build_contracts_from_intent(target_abs, intent_output)
        initial_state["rewrite_contracts"] = [c.model_dump() for c in contracts]
        _log_event(f"Built {len(contracts)} rewrite contracts.", log_file=log_file_abs, verbose=verbose)

        initial_state["target_context_map"] = build_target_context_map(analyses, contracts, target_abs)
        initial_state["read_write_map"] = build_read_write_map(analyses)
    except Exception as e:
        _log_event(f"Error building contracts: {e}", log_file=log_file_abs, verbose=verbose)
        raise

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
        f"Run the ADCo rewriter workflow for the codebase at: {target_abs}.\n\n"
        f"The extracted intent and rewrite contracts are already in session state. "
        f"The workflow copies the codebase to a sandbox, selects strategies, then "
        f"optimizes and deterministically verifies each target function."
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
