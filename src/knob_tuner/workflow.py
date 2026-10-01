"""Deterministic ADK ``Workflow`` for the ADCo knob_tuner pipeline.

Staged graph (Wave 3)::

    START -> validate_budget -> prepare_run -> db_inspector
        -> materialize_inventory -> candidate_generation_agent
        -> compile_candidate -+-ok--> screen_candidate
                               |         -+-pass--> diagnosis_agent
                               |         +--fail--> diagnosis_agent
                               +--rejected--> confirmation_controller
        diagnosis_agent --diagnosed--> confirmation_controller
        confirmation_controller -+-retry--> candidate_generation_agent
                                 +--done---> decision_node
                                     -> production_preflight_node
                                     -> apply_live -> finalize

The LLM sub-agents only recommend, diagnose, and inspect; the staged
``FunctionNode`` wrappers own budgeting, compilation, screening, the
retry loop, and the terminal decision in Python. The diagnosis agent
reviews EVERY screen verdict (pass or fail) and decides STOP (halt) vs
NEXT candidate (shrink/change/drop/adjust/retry strategy); compile
rejections retry cheaply via the controller without an LLM call.
Attempt accounting lives in the outcome producers (screen/compile), so
the controller is a pure router and extra visits never double-count.
Production never restarts automatically.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone
from typing import Any, Union

from google.adk import Context, Event, Workflow
from google.adk.models import BaseLlm
from google.adk.workflow import Edge, FunctionNode

from src.knob_tuner.contracts import (
    ApplyMode,
    KnobPlan,
    KnobScope,
    ResourceBudget,
    RunManifest,
    TuningStatus,
)
from src.knob_tuner.stages import nodes as stage_nodes
from src.knob_tuner.stages.models import (
    CandidateProposal,
    CompiledPlan,
    CompileRejection,
    DiagnosisOutput,
    PreflightVerdict,
    ScreenVerdict,
    TerminalDecision,
)
from src.knob_tuner.sub_agents.candidate_generator.agent import (
    create_candidate_generation_agent,
)
from src.knob_tuner.sub_agents.db_inspector.agent import create_db_inspector_agent
from src.knob_tuner.sub_agents.db_inspector.models import DbInspectorOutput
from src.knob_tuner.sub_agents.diagnosis_agent.agent import create_diagnosis_agent
from src.knob_tuner.tools.benchmark_tools import derive_screen_profile
from src.knob_tuner.tools.db_connector import DBConfig, load_db_config
from src.knob_tuner.tools.db_tools import apply_knobs
from src.knob_tuner.tools.docker_tools import (
    cleanup_snapshot_image,
    resolve_docker_image,
)
from src.knob_tuner.tools.knobs import (
    coerce_apply_mode,
    coerce_db_config,
    coerce_profile,
)
from src.knob_tuner.tools.progress import make_progress_callback
from src.knob_tuner.tools.run_artifacts import application_code_hash
from src.knob_tuner.tools.validation import SnapshotRegistry, validate_plan

DEFAULT_MODEL = "gemini-3.5-flash-lite"


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


def _resolve_db_config(state: Any) -> DBConfig | None:
    """Reconstruct a live :class:`DBConfig` without a secret in session state.

    Session state persists only a redacted, JSON-serializable view (no
    password), so a usable config is rebuilt from the recorded config path. A
    fully-populated config already in state (embedded callers/tests) is honored
    first.
    """
    for key in ("db_config", "production_db_config", "prod_db_config"):
        cfg = coerce_db_config(state.get(key))
        if cfg is not None and cfg.password:
            return cfg

    path = state.get("db_config_path") or state.get("config_path")
    if isinstance(path, str) and os.path.isfile(path):
        db_type = str(state.get("db_type", "postgres") or "postgres")
        db_name = str(
            state.get("database") or state.get("db_name") or state.get("dbname") or ""
        ).strip()
        try:
            return load_db_config(path, db_type=db_type, db_override=db_name or None)
        except Exception:
            return None

    for key in ("db_config", "production_db_config", "prod_db_config"):
        cfg = coerce_db_config(state.get(key))
        if cfg is not None:
            return cfg
    return None


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

    cfg = _resolve_db_config(state)
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

    if str(state.get("apply_mode", "")).strip().lower() == "maintenance-assisted":
        progress("maintenance-assisted: generating manual SQL (no production mutation)...")
        try:
            sql_results = apply_knobs(knobs, cfg, dry_run=True, mode=mode)
            manual_sql = [r.get("sql", "") for r in sql_results if r.get("sql")]
            live_result = {
                "status": "MANUAL_SQL",
                "reason": "maintenance-assisted: manual SQL emitted; no auto restart",
                "applied_knobs": [],
                "persisted_static_knobs": [],
                "pending_restart_knobs": pending_restart,
                "manual_sql": manual_sql,
                "results": sql_results,
            }
        except Exception as exc:
            live_result = {
                "status": "FAILED",
                "reason": f"manual SQL generation error: {exc}",
                "applied_knobs": [],
                "persisted_static_knobs": [],
                "pending_restart_knobs": pending_restart,
                "manual_sql": [],
            }
        # Manual SQL is not an applied success: never leave result_status PASS.
        apply_status = (
            TuningStatus.INCONCLUSIVE.value
            if live_result["status"] == "MANUAL_SQL"
            else TuningStatus.FAIL.value
        )
        return Event(
            output=live_result,
            state={
                "live_result": live_result,
                "applied_knobs": [],
                "result_status": apply_status,
            },
        )

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

    apply_state: dict[str, Any] = {
        "live_result": live_result,
        "applied_knobs": live_result["applied_knobs"],
    }
    if live_result["status"] == "FAILED":
        apply_state["result_status"] = TuningStatus.FAIL.value
    return Event(output=live_result, state=apply_state)


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

    # Reconcile against the live apply: a validated PASS is only a success if
    # the production mutation actually happened. A failed or absent apply must
    # never surface as a PASS manifest. MANUAL_SQL is not an applied success.
    live = state.get("live_result") or {}
    live_status = str(live.get("status", "") or "").upper()
    errors = list(state.get("staging_issues", []) or [])
    if live_status in ("FAILED", "ERROR"):
        reason = str(live.get("reason", "") or "")
        errors.append(f"live apply {live_status}: {reason}".rstrip(": "))
    if status == TuningStatus.PASS and live_status not in ("APPLIED", "COMPLETED"):
        status = (
            TuningStatus.FAIL
            if live_status in ("FAILED", "ERROR")
            else TuningStatus.INCONCLUSIVE
        )

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
        errors=errors,
        final_status=status.value,
    )

    return Event(
        output=manifest.model_dump(),
        state={
            "run_manifest": manifest.model_dump(),
            "result_status": status.value,
        },
    )


# ---------------------------------------------------------------------------
# Staged graph nodes (Wave 3)
# ---------------------------------------------------------------------------
# Thin wrappers over ``src.knob_tuner.stages.nodes``. Each is wrapped as a
# ``FunctionNode`` with ``rerun_on_resume=True``; ``node_input`` is typed
# with the Wave-1 boundary model (``input_schema`` inference handles the
# rest — union/``dict`` hints intentionally leave it unset). Routed nodes
# emit plain-string routes on ``ctx`` so the graph can branch.


def prepare_run_node(ctx: Context) -> dict[str, Any]:
    """Resolve run configuration into state (read-only, no benchmarking)."""
    return stage_nodes.prepare_run(ctx)


def materialize_inventory_node(
    ctx: Context, node_input: DbInspectorOutput
) -> dict[str, Any]:
    """Persist the db_inspector output to state + ``knobs.json``."""
    return stage_nodes.materialize_inventory(ctx, node_input)


def compile_candidate_node(
    ctx: Context, node_input: CandidateProposal
) -> CompiledPlan | CompileRejection:
    """Compile one proposal; route ``"ok"`` or ``"rejected"``."""
    result = stage_nodes.compile_candidate(ctx, node_input)
    if isinstance(result, CompileRejection):
        ctx.route = "rejected"
        ctx.state["exp_name"] = result.design_name or "experiment"
    else:
        ctx.route = "ok"
        ctx.state["exp_name"] = result.exp_name
        ctx.state["phase"] = result.phase
        ctx.state["n_knobs"] = len(result.valid_knobs)
    return result


def _screen_runtime(ctx: Context) -> tuple[Any, Any]:
    """Build the ``(validate_fn, run_profile)`` pair ``screen_candidate`` needs.

    Mirrors the read-only plumbing of the former closed-loop tuner: the screen
    dataset is derived from the inspected target's row estimates, every arm
    measures the same short-run window, and one snapshot registry is shared
    across screens via state.
    """
    state = ctx.state
    progress = make_progress_callback(
        state.get("log_file"), bool(state.get("verbose", False))
    )
    budget = ResourceBudget(**(state.get("resource_budget") or {}))
    profile = coerce_profile(state.get("sysbench_profile"))
    run_id = state.get("run_id", "") or ""
    run_dir = state.get("run_dir", "") or ""
    db_type = state.get("db_type", "postgres") or "postgres"
    db_version = state.get("db_version")
    database = (
        state.get("database") or state.get("db_name") or state.get("dbname") or ""
    )
    benchmark_kind = str(state.get("screening_benchmark", "sysbench") or "sysbench")
    dry_run = bool(state.get("dry_run", False))
    candidate_reps = max(2, int(state.get("candidate_repetitions", 10) or 10))
    candidate_seconds = max(
        1, int(state.get("candidate_measurement_seconds", 10) or 10)
    )
    candidate_warmup = max(
        0, int(state.get("candidate_warmup_seconds", 2) or 2)
    )
    try:
        min_improvement = float(getattr(profile, "min_improvement_pct", 5.0))
    except (TypeError, ValueError):
        min_improvement = 5.0
    state["min_improvement_pct"] = min_improvement

    schema_info = state.get("schema_info") or []
    target_total_rows = 0
    if isinstance(schema_info, list):
        for table in schema_info:
            if not isinstance(table, dict):
                continue
            try:
                target_total_rows += max(
                    0, int(table.get("approximate_row_count", 0) or 0)
                )
            except (TypeError, ValueError):
                continue
    explicit_rows = int(state.get("screen_total_rows", 0) or 0)
    if explicit_rows > 0:
        target_total_rows = explicit_rows
    max_rows = int(state.get("screen_max_rows", 5_000_000) or 5_000_000)
    screen_profile, dataset_meta = derive_screen_profile(
        profile, target_total_rows or None, max_total_rows=max_rows
    )
    if explicit_rows > 0:
        dataset_meta["source"] = "explicit"
    state["screen_dataset"] = dataset_meta
    run_profile = screen_profile.model_copy(
        update={
            "repetitions": candidate_reps,
            "measurement_seconds": max(1, candidate_seconds),
            "warmup_seconds": max(0, candidate_warmup),
        }
    )

    registry = state.get("snapshot_registry")
    if not isinstance(registry, SnapshotRegistry):
        registry = SnapshotRegistry()
        state["snapshot_registry"] = registry

    def _validate(
        plan: KnobPlan,
        run_profile: Any,
        shared_baseline: Any,
        attempt: int,
        early_stop_min_reps: Any = None,
        baseline_only: bool = False,
    ) -> dict[str, Any]:
        return validate_plan(
            run_id=run_id,
            run_dir=run_dir,
            plan=plan,
            budget=budget,
            profile=run_profile,
            db_type=db_type,
            db_version=db_version,
            database=database,
            # Staging validates the FULL plan (postmaster knobs included) by
            # restarting the throwaway container; production apply is scoped
            # separately by safe-auto / maintenance-assisted.
            apply_mode=ApplyMode.PERSIST_STATIC,
            dry_run=dry_run,
            attempt=attempt,
            benchmark_kind=benchmark_kind,
            progress=progress,
            snapshot=registry,
            reversal=False,
            shared_baseline=shared_baseline,
            baseline_only=baseline_only,
            early_stop_min_reps=early_stop_min_reps,
        )

    return _validate, run_profile


def screen_candidate_node(
    ctx: Context, node_input: CompiledPlan
) -> ScreenVerdict:
    """Screen one compiled plan; route ``"pass"``/``"fail"`` to diagnosis.

    Both routes point at the diagnosis agent (distinct strings kept): every
    screen verdict — pass or fail — is reviewed by diagnosis, which decides
    STOP vs NEXT candidate. Outcome accounting (history append + attempt
    bump) happens inside ``screen_candidate`` itself.
    """
    validate_fn, run_profile = _screen_runtime(ctx)
    verdict = stage_nodes.screen_candidate(
        ctx, node_input, validate_fn=validate_fn, run_profile=run_profile
    )
    if str(verdict.status).upper() == "PASS" and verdict.confirmed:
        ctx.route = "pass"
    else:
        ctx.route = "fail"
    return verdict


def confirmation_controller_node(
    ctx: Context,
    node_input: ScreenVerdict | CompileRejection | DiagnosisOutput,
) -> dict[str, Any]:
    """Pure router over diagnosed outcomes; route ``"retry"``/``"done"``.

    Never counts attempts and never appends outcome history (screen and
    compile already recorded their outcomes exactly once). Reads the latest
    diagnosis correction plus attempt-vs-cap plus the confident-win
    backstop: ``stop`` → done via the two-score gate (diagnosis confidence
    + Welch P(win) must back the stated stop_reason; disagreement routes
    retry with ``diag_stat_disagree``), confident win →
    done (confident_win_backstop), cap → done (attempt_cap), else retry.
    ``CompileRejection`` inputs arrive directly from compile (cheap retry,
    no LLM call); screen verdicts always arrive via diagnosis first.
    """
    return stage_nodes.confirmation_controller(ctx, node_input)


_DECISION_STATUS = {
    "apply_winner": TuningStatus.PASS.value,
    "keep_best": TuningStatus.PASS.value,
    "inconclusive": TuningStatus.INCONCLUSIVE.value,
    "fail": TuningStatus.FAIL.value,
}


def decision_node(ctx: Context) -> TerminalDecision:
    """Pick the terminal outcome and bridge it into apply/finalize state keys."""
    term = stage_nodes.decision(ctx)
    status = _DECISION_STATUS.get(term.decision, TuningStatus.INCONCLUSIVE.value)
    winner = dict(term.winner_plan or {})
    summary = term.summary or {}
    try:
        plan_hash = KnobPlan.model_validate(winner).plan_hash() if winner else ""
    except ValueError:
        plan_hash = ""
    state = ctx.state
    state["knob_plan"] = winner if winner else {"knobs": []}
    state["knob_plan_hash"] = plan_hash or str(summary.get("plan_hash", "") or "")
    state["result_status"] = status
    state["staging_validated"] = status == TuningStatus.PASS.value
    state["staging_issues"] = [str(r) for r in (summary.get("reasons", []) or [])]
    state["validation_attempts"] = list(state.get("experiment_history") or [])
    state["candidate_archive"] = summary.get("archive", {}) or {}
    state["improvement_confident"] = bool(
        summary.get("improvement_confident", False)
    )
    registry = state.get("snapshot_registry")
    if isinstance(registry, SnapshotRegistry):
        for image in registry.images():
            with contextlib.suppress(Exception):
                cleanup_snapshot_image(image)
    return term


def production_preflight_node(
    ctx: Context, node_input: TerminalDecision
) -> PreflightVerdict:
    """Route the terminal plan; recorded only, production is never mutated here."""
    verdict = stage_nodes.production_preflight(ctx, node_input)
    state = ctx.state
    state["preflight_verdict"] = verdict.model_dump()
    state["preflight_route"] = verdict.route
    if verdict.route == "blocked" and str(
        state.get("result_status", "") or ""
    ).upper() == TuningStatus.PASS.value:
        state["result_status"] = TuningStatus.INCONCLUSIVE.value
        issues = state.setdefault("staging_issues", [])
        if isinstance(issues, list) and verdict.reason not in issues:
            issues.append(f"preflight blocked: {verdict.reason}")
    return verdict


def create_knob_tuner_workflow(
    model: Union[str, BaseLlm] = DEFAULT_MODEL,
    buffer_time: float = 0.0,
) -> Workflow:
    """Create the deterministic staged ADCo knob_tuner workflow graph."""
    inspector_agent = create_db_inspector_agent(model, buffer_time)
    candidate_agent = create_candidate_generation_agent(model, buffer_time)
    diagnosis_agent = create_diagnosis_agent(model, buffer_time)

    prepare_node = FunctionNode(
        func=prepare_run_node, name="prepare_run", rerun_on_resume=True
    )
    materialize_node = FunctionNode(
        func=materialize_inventory_node,
        name="materialize_inventory",
        rerun_on_resume=True,
    )
    compile_node = FunctionNode(
        func=compile_candidate_node,
        name="compile_candidate",
        rerun_on_resume=True,
    )
    screen_node = FunctionNode(
        func=screen_candidate_node,
        name="screen_candidate",
        rerun_on_resume=True,
    )
    controller_node = FunctionNode(
        func=confirmation_controller_node,
        name="confirmation_controller",
        rerun_on_resume=True,
    )
    decide_node = FunctionNode(
        func=decision_node, name="decision_node", rerun_on_resume=True
    )
    preflight_node = FunctionNode(
        func=production_preflight_node,
        name="production_preflight_node",
        rerun_on_resume=True,
    )

    return Workflow(
        name="adco_knob_tuner",
        description=(
            "ADCo knob_tuner — staged recommend/compile/screen/diagnose/"
            "decide/preflight/apply workflow."
        ),
        edges=[
            ("START", validate_budget_node, prepare_node, inspector_agent),
            (inspector_agent, materialize_node),
            (materialize_node, candidate_agent),
            (candidate_agent, compile_node),
            (compile_node, {"ok": screen_node, "rejected": controller_node}),
            # Every screen verdict (pass AND fail, distinct route strings)
            # goes to diagnosis. One edge carrying both routes: the graph
            # validator dedupes on (from, to), so two dict entries to the
            # same target would read as a duplicate edge.
            Edge(from_node=screen_node, to_node=diagnosis_agent,
                 route=["pass", "fail"]),
            (diagnosis_agent, controller_node),
            (controller_node, {"retry": candidate_agent, "done": decide_node}),
            (decide_node, preflight_node),
            (preflight_node, apply_live_node),
            (apply_live_node, finalize_node),
        ],
    )
