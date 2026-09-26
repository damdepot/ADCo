"""Deterministic ADK ``Workflow`` for the ADCo knob_tuner pipeline.

Replaces the former LLM-orchestrated root agent with a Python-owned graph:

    START -> validate_budget -> db_inspector -> tune_loop -> apply_live -> finalize

``tune_loop`` is an async-generator :class:`FunctionNode` that owns the
recommendation/validation retry budget in Python. The LLM sub-agents only
recommend and inspect; the deterministic ``validate_plan`` tool decides
PASS/FAIL/INCONCLUSIVE, and production never restarts automatically.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any, Union

from google.adk import Context, Event, Workflow
from google.adk.models import BaseLlm
from google.adk.workflow import FunctionNode

from src.knob_tuner.contracts import (
    KnobPlan,
    KnobScope,
    ResourceBudget,
    RunManifest,
    TuningStatus,
)
from src.knob_tuner.sub_agents.db_inspector.agent import create_db_inspector_agent
from src.knob_tuner.sub_agents.knob_recommender.agent import (
    create_knob_recommender_agent,
)
from src.knob_tuner.tools.db_tools import apply_knobs
from src.knob_tuner.tools.docker_tools import resolve_docker_image
from src.knob_tuner.tools.knob_scope import fetch_pg_settings_context
from src.knob_tuner.tools.knobs import (
    build_plan,
    coerce_apply_mode,
    coerce_db_config,
    coerce_profile,
    load_raw_knobs,
)
from src.knob_tuner.tools.progress import make_progress_callback
from src.knob_tuner.tools.run_artifacts import application_code_hash
from src.knob_tuner.tools.validation import validate_plan

DEFAULT_MODEL = "gemini-3.5-flash-lite"


def _recommendation_instruction(
    attempt: int, last_reasons: list[str], last_result: dict[str, Any]
) -> str:
    """Build the retry-aware instruction handed to the knob_recommender agent."""
    parts = [
        "Recommend database configuration knobs for the target database.",
        "Use the workload, resource budget, and inspected settings already in "
        "session state.",
    ]
    if attempt > 1:
        parts.append(f"This is validation retry attempt {attempt}.")
        if last_reasons:
            parts.append(
                "The previous attempt failed for these reasons: "
                + "; ".join(str(reason) for reason in last_reasons)
                + "."
            )
        paired = (last_result or {}).get("paired") or {}
        if paired:
            parts.append(
                "Benchmark delta: baseline_median_tps={} tuned_median_tps={} "
                "delta_pct={} p95_delta_pct={}.".format(
                    paired.get("median_tps_baseline"),
                    paired.get("median_tps_tuned"),
                    paired.get("delta_pct"),
                    paired.get("p95_delta_pct"),
                )
            )
        parts.append(
            "Revise the recommendations to remediate the failures and avoid "
            "over-allocating memory."
        )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def validate_budget_node(ctx: Context, node_input: Any = None) -> Event:
    """Validate the resource budget before any side effect can occur."""
    raw = ctx.state.get("resource_budget")
    if raw is None:
        raise ValueError(
            "resource_budget is required in session state; pass --cpu-cores and "
            "--memory (no host auto-detection is performed)."
        )

    try:
        if isinstance(raw, ResourceBudget):
            budget = raw
        else:
            budget = ResourceBudget.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"invalid resource_budget: {exc}") from exc

    return Event(
        output=budget.model_dump(),
        state={"resource_budget": budget.model_dump()},
    )


def make_tune_loop(recommender_node: Any, validator: Any):
    """Build the Python-owned recommend/validate retry loop node.

    The returned async generator keeps *recommender_node* and *validator* in its
    closure so it can be unit-tested without the ADK runtime.
    """

    async def tune_loop(
        ctx: Context, node_input: Any = None
    ) -> AsyncGenerator[Any, None]:
        state = ctx.state
        progress = make_progress_callback(
            state.get("log_file"), bool(state.get("verbose", False))
        )

        budget = ResourceBudget(**(state.get("resource_budget") or {}))
        profile = coerce_profile(state.get("sysbench_profile"))
        apply_mode = coerce_apply_mode(state.get("apply_mode", "dynamic"))
        max_attempts = int(state.get("max_validation_attempts", 4) or 4)
        dry_run = bool(state.get("dry_run", False))
        run_id = state.get("run_id", "") or ""
        run_dir = state.get("run_dir", "") or ""
        db_type = state.get("db_type", "postgres") or "postgres"
        db_version = state.get("db_version")
        database = (
            state.get("database")
            or state.get("db_name")
            or state.get("dbname")
            or ""
        )

        try:
            db_config = coerce_db_config(state.get("db_config"))
            context_map = (
                fetch_pg_settings_context(db_config)
                if db_config is not None
                else {}
            )
        except Exception:
            context_map = {}

        attempts: list[dict[str, Any]] = []
        last_reasons: list[str] = []
        last_result: dict[str, Any] = {}
        plan = KnobPlan(knobs=[])
        plan_hash = plan.plan_hash()
        status = TuningStatus.INCONCLUSIVE

        for attempt in range(1, max_attempts + 1):
            yield Event(
                state={
                    "validation_attempt_count": attempt,
                    "last_failure": last_reasons,
                }
            )

            progress(
                f"Attempt {attempt}/{max_attempts} — requesting knob recommendation..."
            )
            await ctx.run_node(
                recommender_node,
                node_input=_recommendation_instruction(
                    attempt, last_reasons, last_result
                ),
            )

            raw_knobs = load_raw_knobs(state)
            plan = build_plan(raw_knobs, context_map)
            plan_hash = plan.plan_hash()
            progress(
                f"Recommendation received: {len(plan.knobs)} knobs "
                f"({', '.join(spec.name for spec in plan.knobs)})"
            )

            result = validator(
                run_id=run_id,
                run_dir=run_dir,
                plan=plan,
                budget=budget,
                profile=profile,
                db_type=db_type,
                db_version=db_version,
                database=database,
                apply_mode=apply_mode,
                dry_run=dry_run,
                attempt=attempt,
                progress=progress,
            )
            last_result = result or {}

            attempt_status = TuningStatus(
                str(last_result.get("status", TuningStatus.FAIL.value)).upper()
            )
            reasons = list(last_result.get("reasons", []) or [])
            last_reasons = reasons
            progress(
                f"Attempt {attempt}/{max_attempts} result: {attempt_status.value}"
                + (f" — {'; '.join(str(r) for r in reasons)}" if reasons else "")
            )

            attempts.append(
                {
                    "attempt": attempt,
                    "status": attempt_status.value,
                    "reasons": reasons,
                    "plan_hash": plan_hash,
                    "paired": last_result.get("paired"),
                }
            )

            if attempt_status == TuningStatus.PASS:
                status = TuningStatus.PASS
                break

            if last_result.get("paired") is None and not dry_run:
                status = TuningStatus.FAIL
                break

            status = attempt_status

        attestation = last_result.get("attestation")
        summary = {
            "status": status.value,
            "run_id": run_id,
            "plan_hash": plan_hash,
            "attempt_count": len(attempts),
            "reasons": last_reasons,
            "paired": last_result.get("paired"),
        }

        yield Event(
            output=summary,
            state={
                "knob_plan": plan.model_dump(),
                "knob_plan_hash": plan_hash,
                "validation_attestation": attestation,
                "staging_validated": status == TuningStatus.PASS,
                "result_status": status.value,
                "validation_attempts": attempts,
                "staging_issues": last_reasons,
            },
        )

    return tune_loop


def apply_live_node(ctx: Context, node_input: Any = None) -> Event:
    """Apply the validated plan to the live database (never auto-restart)."""
    state = ctx.state
    progress = make_progress_callback(
        state.get("log_file"), bool(state.get("verbose", False))
    )
    dry_run = bool(state.get("dry_run", False))
    status = str(state.get("result_status", "") or "").upper()

    if dry_run or status != TuningStatus.PASS.value:
        live_result = {
            "status": "SKIPPED",
            "reason": (
                "dry-run: live mutations skipped"
                if dry_run
                else f"validation status is {status or 'UNKNOWN'}, not PASS"
            ),
            "applied_knobs": [],
            "persisted_static_knobs": [],
            "pending_restart_knobs": [],
        }
        progress(f"live apply skipped: {live_result['reason']}")
        return Event(
            output=live_result,
            state={"live_result": live_result, "applied_knobs": []},
        )

    cfg = coerce_db_config(state.get("db_config")) or coerce_db_config(
        state.get("production_db_config")
    )
    if cfg is None:
        live_result = {
            "status": "SKIPPED",
            "reason": "no live database configuration available in state",
            "applied_knobs": [],
            "persisted_static_knobs": [],
            "pending_restart_knobs": [],
        }
        progress(f"live apply skipped: {live_result['reason']}")
        return Event(
            output=live_result,
            state={"live_result": live_result, "applied_knobs": []},
        )

    raw_plan = state.get("knob_plan") or {}
    try:
        plan = (
            raw_plan
            if isinstance(raw_plan, KnobPlan)
            else KnobPlan.model_validate(raw_plan)
        )
    except Exception:
        plan = KnobPlan(knobs=[])

    mode = coerce_apply_mode(state.get("apply_mode", "dynamic"))
    knobs = [spec.model_dump() for spec in plan.knobs]

    pending_restart = [
        spec.model_dump()
        for spec in plan.knobs
        if spec.restart_required or spec.scope == KnobScope.POSTMASTER
    ]

    try:
        progress(f"Applying plan to live DB ({mode.value})...")
        results = apply_knobs(knobs, cfg, dry_run=False, mode=mode)
        applied = [r for r in results if r.get("status") == "applied"]
        restart_names = {
            spec.name
            for spec in plan.knobs
            if spec.restart_required or spec.scope == KnobScope.POSTMASTER
        }
        persisted_static = [r for r in applied if r.get("knob") in restart_names]
        progress(
            f"applied {len(applied)}; pending restart: "
            f"{', '.join(sorted(restart_names)) or 'none'}"
        )
        live_result = {
            "status": "APPLIED" if applied else "COMPLETED",
            "reason": "",
            "applied_knobs": applied,
            "persisted_static_knobs": persisted_static,
            "pending_restart_knobs": pending_restart,
            "results": results,
        }
    except Exception as exc:
        live_result = {
            "status": "FAILED",
            "reason": f"live apply error: {exc}",
            "applied_knobs": [],
            "persisted_static_knobs": [],
            "pending_restart_knobs": pending_restart,
        }

    return Event(
        output=live_result,
        state={
            "live_result": live_result,
            "applied_knobs": live_result["applied_knobs"],
        },
    )


def finalize_node(ctx: Context, node_input: Any = None) -> Event:
    """Compose and persist the run manifest from session state."""
    state = ctx.state
    progress = make_progress_callback(
        state.get("log_file"), bool(state.get("verbose", False))
    )

    try:
        status = TuningStatus(
            str(state.get("result_status", TuningStatus.INCONCLUSIVE.value)).upper()
        )
    except ValueError:
        status = TuningStatus.INCONCLUSIVE

    profile = coerce_profile(state.get("sysbench_profile"))
    target = state.get("target", "") or ""
    db_type = state.get("db_type", "") or ""
    db_version = state.get("db_version") or ""

    db_image = state.get("db_image") or ""
    if not db_image:
        try:
            db_image = resolve_docker_image(db_type, db_version)
        except Exception:
            db_image = ""

    attestation = state.get("validation_attestation") or {}
    if isinstance(attestation, dict):
        verified_knobs = attestation.get("verified_knobs", []) or []
    else:
        verified_knobs = getattr(attestation, "verified_knobs", []) or []

    attempts = state.get("validation_attempts") or []
    attempt_count = len(attempts) or int(state.get("validation_attempt_count", 0) or 0)

    progress(f"run complete: {status.value}")
    manifest = RunManifest(
        run_id=state.get("run_id", "") or "",
        timestamp=datetime.now(timezone.utc).isoformat(),
        status=status,
        resource_budget=state.get("resource_budget") or {},
        db_engine=db_type,
        db_version=str(db_version),
        db_image=db_image,
        application_target=target,
        application_code_hash=application_code_hash(target),
        knob_plan_hash=state.get("knob_plan_hash", "") or "",
        sysbench_profile_hash=profile.profile_hash(),
        seed=profile.seed,
        client_threads=profile.threads,
        attempt_count=attempt_count,
        applied_knobs=state.get("applied_knobs", []) or [],
        verified_knobs=verified_knobs,
        errors=list(state.get("staging_issues", []) or []),
        final_status=status.value,
    )

    return Event(
        output=manifest.model_dump(),
        state={
            "run_manifest": manifest.model_dump(),
            "result_status": status.value,
        },
    )


def create_knob_tuner_workflow(
    model: Union[str, BaseLlm] = DEFAULT_MODEL,
    buffer_time: float = 0.0,
) -> Workflow:
    """Create the deterministic ADCo knob_tuner workflow graph."""
    inspector_agent = create_db_inspector_agent(model, buffer_time)
    recommender_agent = create_knob_recommender_agent(model, buffer_time)

    tune_loop_node = FunctionNode(
        func=make_tune_loop(recommender_agent, validate_plan),
        name="tune_loop",
        rerun_on_resume=True,
    )

    return Workflow(
        name="adco_knob_tuner",
        description=(
            "ADCo knob_tuner — deterministic recommend/validate/apply workflow."
        ),
        edges=[
            (
                "START",
                validate_budget_node,
                inspector_agent,
                tune_loop_node,
                apply_live_node,
                finalize_node,
            )
        ],
    )
