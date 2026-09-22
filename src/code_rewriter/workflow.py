"""ADCo rewriter workflow — per-target optimize/verify orchestration.

Replaces the former LLM-driven root orchestrator with a deterministic ADK
``Workflow`` graph:

    START -> copy -> strategies -> prepare -> orchestrate -> verifier -> finalize

``copy``/``strategies`` reuse the existing sandbox + planner tools. ``prepare``
seeds per-target loop state. ``orchestrate`` runs the code optimizer and the
deterministic target verifier once per target function, retrying up to
``MAX_ATTEMPTS_PER_TARGET`` times. The ``verifier`` sub-agent performs the final
LLM review, and ``finalize`` composes the verdict — a deterministic FAIL can
never be overridden into a PASS by the LLM review.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, Union

from google.adk import Context, Event, Workflow
from google.adk.models import BaseLlm
from google.adk.workflow import FunctionNode

from src.code_rewriter._common import _maybe_parse
from src.code_rewriter.models.rewrite_models import RewriteContract
from src.code_rewriter.sub_agents.code_optimizer.agent import create_code_optimizer_agent
from src.code_rewriter.sub_agents.verifier.agent import create_verifier_agent
from src.code_rewriter.tools import copy_to_sandbox, get_optimization_strategies
from src.code_rewriter.tools.pipeline_analysis import verify_target

MAX_ATTEMPTS_PER_TARGET = 3


def attempts_exhausted(attempts: int) -> bool:
    """Return True once *attempts* reaches the per-target retry budget."""
    return attempts >= MAX_ATTEMPTS_PER_TARGET


def copy_node(ctx: Context, node_input: Any = None) -> Event:
    """Copy the target codebase into the sandbox and record the sandbox path."""
    msg = copy_to_sandbox(ctx)
    return Event(output=msg, state={"sandbox": ctx.state.get("sandbox", "")})


def strategies_node(ctx: Context, node_input: Any = None) -> Event:
    """Select applicable optimization strategies from the extracted intent."""
    msg = get_optimization_strategies(ctx)
    return Event(output=msg)


def prepare(ctx: Context, node_input: Any = None) -> Event:
    """Seed the per-target loop state before orchestration begins."""
    contracts = ctx.state.get("rewrite_contracts", []) or []
    return Event(
        output={"total": len(contracts)},
        state={
            "target_index": 0,
            "target_results": [],
            "current_contract": (contracts[0] if contracts else None),
            "last_failure": None,
        },
    )


def verify_target_node(ctx: Context, node_input: Any = None) -> dict:
    """Deterministically verify the current target contract in the sandbox."""
    raw = ctx.state.get("current_contract")
    contract = RewriteContract(**raw) if isinstance(raw, dict) else raw
    result = verify_target(
        ctx.state.get("target", ""),
        ctx.state.get("sandbox", ""),
        contract,
    )
    return result.model_dump()


def make_orchestrate(
    optimizer_node: Any,
    verify_node: Any,
    max_attempts: int = MAX_ATTEMPTS_PER_TARGET,
):
    """Build the per-target optimize/verify orchestrator.

    The returned async generator keeps *optimizer_node* and *verify_node* in its
    closure so it can be unit-tested without the ADK runtime.
    """

    async def orchestrate(
        ctx: Context, node_input: Any = None
    ) -> AsyncGenerator[Any, None]:
        contracts = ctx.state.get("rewrite_contracts", []) or []
        results: list[dict[str, Any]] = []

        for contract in contracts:
            target = contract.get("target") or {}
            qfn = target.get("qualified_function") or target.get("function") or ""
            passed = False
            failure: Any = None
            verdict: Any = None

            for attempt in range(1, max_attempts + 1):
                yield Event(
                    state={
                        "current_contract": contract,
                        "target_index": len(results),
                        "attempt_count": attempt,
                        "last_failure": failure,
                    }
                )
                await ctx.run_node(
                    optimizer_node,
                    node_input=(
                        f"Optimize target function {qfn}. Follow the Acceptance "
                        f"Checklist from get_optimization_context."
                    ),
                )
                verdict = await ctx.run_node(verify_node)
                if isinstance(verdict, dict) and verdict.get("status") == "PASS":
                    passed = True
                    break
                failure = verdict

            results.append(
                {
                    "file": target.get("file", ""),
                    "function": qfn,
                    "status": "PASS" if passed else "FAIL",
                    "verification": verdict,
                }
            )

        yield Event(output={"results": results}, state={"target_results": results})

    return orchestrate


def finalize(ctx: Context, node_input: Any = None) -> Event:
    """Compose the final verdict, preserving deterministic FAILs over LLM PASSes."""
    results = ctx.state.get("target_results", []) or []
    n_total = len(results)
    n_pass = sum(1 for r in results if r.get("status") == "PASS")
    all_pass = all(r.get("status") == "PASS" for r in results)

    errors = [
        v
        for r in results
        for v in ((r.get("verification") or {}).get("violations") or [])
        if v.get("severity") == "ERROR"
    ]
    warnings = [
        v
        for r in results
        for v in ((r.get("verification") or {}).get("violations") or [])
        if v.get("severity") == "WARNING"
    ]

    det = {
        "status": "PASS" if all_pass else "FAIL",
        "summary": f"{n_pass}/{n_total} target functions transformed.",
        "expected_targets": n_total,
        "transformed_targets": n_pass,
        "missing_targets": n_total - n_pass,
        "rewrite_coverage": (n_pass / n_total) if n_total else 1.0,
        "target_coverage": [
            {
                "file": r.get("file", ""),
                "function": r.get("function", ""),
                "status": (
                    "TRANSFORMED" if r.get("status") == "PASS" else "MISSING_REWRITE"
                ),
                "details": (
                    (r.get("verification") or {}).get("summary", "")
                    if r.get("status") != "PASS"
                    else "Target function transformed."
                ),
            }
            for r in results
        ],
        "violations": errors + warnings,
    }

    llm = _maybe_parse(ctx.state.get("verifier_output"))

    if not all_pass:
        vo = {
            "status": "FAIL",
            "category": (
                llm.get("category")
                if llm.get("category") not in (None, "", "NONE")
                else "strategy_not_applied"
            ),
            "reason": det["summary"],
            "detail": "; ".join(
                f"{r.get('file')}::{r.get('function')}"
                for r in results
                if r.get("status") != "PASS"
            ),
            "suggestion": llm.get("suggestion", "")
            or (
                "Apply the Acceptance Checklist: remove every DB call from loops "
                "and hoist batch reads/writes."
            ),
        }
    else:
        # Deterministic verification is authoritative: every target transformed
        # means PASS. The advisory LLM review may add a suggestion, but it must
        # not flip a deterministic PASS into a FAIL (observed hallucinated
        # "residual loop ops" on already-batched code). Advisory warnings are
        # surfaced in `detail`; they never change `status`.
        vo = {
            "status": "PASS",
            "category": "NONE",
            "reason": det["summary"],
            "detail": "; ".join(
                f"[{w.get('code')}] {w.get('message')}" for w in warnings
            ),
            "suggestion": llm.get("suggestion", ""),
        }

    return Event(
        output=vo,
        state={"verifier_output": vo, "deterministic_verification": det},
    )


def create_rewriter_workflow(
    model: Union[str, BaseLlm] = "gemini-3.5-flash-lite",
    buffer_time: float = 0.0,
) -> Workflow:
    """Create the ADCo rewriter workflow graph."""
    optimizer = create_code_optimizer_agent(model, buffer_time=buffer_time)
    verifier = create_verifier_agent(model, buffer_time=buffer_time)

    orchestrate_node = FunctionNode(
        func=make_orchestrate(optimizer, verify_target_node),
        name="orchestrate",
        rerun_on_resume=True,
    )

    return Workflow(
        name="adco_rewriter",
        description="ADCo rewriter — per-target optimize/verify workflow.",
        edges=[
            (
                "START",
                copy_node,
                strategies_node,
                prepare,
                orchestrate_node,
                verifier,
                finalize,
            )
        ],
    )
