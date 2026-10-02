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
from typing import Any, Union

from google.adk import Context, Event, Workflow
from google.adk.models import BaseLlm
from google.adk.workflow import Edge, FunctionNode

from src.knob_tuner.contracts import (
    ApplyMode,
    KnobPlan,
    ResourceBudget,
    TuningStatus,
    get_database_name,
    get_db_config_path,
    get_measure_reps,
    get_measure_seconds,
    get_measure_warmup_seconds,
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
)
from src.knob_tuner.tools.knobs import (
    coerce_apply_mode,
    coerce_db_config,
    coerce_profile,
)
from src.knob_tuner.tools.knob_scope import requires_restart
from src.knob_tuner.tools.progress import make_progress_callback
from src.knob_tuner.tools.run_artifacts import build_run_manifest
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
    for key in ("db_config",):
        cfg = coerce_db_config(state.get(key))
        if cfg is not None and cfg.password:
            return cfg

    path = get_db_config_path(state)
    if path and os.path.isfile(path):
        db_type = str(state.get("db_type", "postgres") or "postgres")
        db_name = get_database_name(state)
        try:
            return load_db_config(path, db_type=db_type, db_override=db_name or None)
        except Exception:
            return None

    for key in ("db_config",):
        cfg = coerce_db_config(state.get(key))
        if cfg is not None:
            return cfg
    return None


def _format_database_identity(db_type: str, host: Any, port: Any, database: Any) -> str:
    """Render a canonical ``engine://host:port/database`` identity string."""
    engine = str(db_type or "").strip().lower() or "unknown"
    return f"{engine}://{host}:{port}/{database}"


def _live_target_identity(cfg: DBConfig, db_type: str = "") -> str:
    """Build the live apply-target identity in attestation format."""
    engine = str(cfg.db_type or db_type or "").strip().lower()
    return _format_database_identity(engine, cfg.host, cfg.port, cfg.database)


def _attestation_identity(attestation: Any) -> str:
    """Best-effort extraction of ``database_identity`` from an attestation."""
    if attestation is None:
        return ""
    if isinstance(attestation, dict):
        return str(attestation.get("database_identity", "") or "")
    return str(getattr(attestation, "database_identity", "") or "")


def _staging_identity_from_state(state: Any) -> str:
    """Return the database identity recorded at staging/validation time."""
    getter = getattr(state, "get", None)
    if not callable(getter):
        return ""
    recorded = str(getter("staging_database_identity", "") or "").strip()
    if recorded:
        return recorded
    return _attestation_identity(getter("validation_attestation")).strip()


def _identity_key(identity: str) -> tuple[str, str]:
    """Reduce an identity string to the comparable ``(engine, database)`` pair.

    Only the engine and database name are compared: staging runs in an
    ephemeral container (dynamic host port), so host/port can never match the
    live target. The database NAME is what staging and production share, and
    applying knobs validated on one database to another is the hazard.
    """
    text = str(identity or "").strip()
    engine, _, rest = text.partition("://")
    database = rest.rsplit("/", 1)[-1].strip() if rest else ""
    engine_norm = engine.strip().lower()
    if engine_norm in ("postgres", "postgresql"):
        engine_norm = "postgresql"
    elif engine_norm in ("mysql", "mariadb"):
        engine_norm = "mysql"
    return (engine_norm, database.lower())


def _staging_identity_matches(staging_identity: str, live_identity: str) -> bool:
    """Whether the staged database matches the live apply target.

    An absent staging record means there is nothing to check against (proceed).
    Otherwise an exact string match passes, else the ``(engine, database)``
    pair must agree; an unparseable record that cannot prove a mismatch does
    not block the apply.
    """
    if not str(staging_identity or "").strip():
        return True
    if not str(live_identity or "").strip():
        return False
    if staging_identity.strip() == live_identity.strip():
        return True
    staged = _identity_key(staging_identity)
    live = _identity_key(live_identity)
    if not staged[1] or not live[1]:
        return True
    return staged == live


def apply_live_node(ctx: Context, node_input: Any = None) -> Event:
    """Apply the validated plan to the live database (never auto-restart)."""
    state = ctx.state
    progress = make_progress_callback(
        state.get("log_file"), bool(state.get("verbose", False))
    )
    dry_run = bool(state.get("dry_run", False))
    status = str(state.get("result_status", "") or "").upper()

    if dry_run or status != TuningStatus.PASS.value:
        # Phase 1.7: a failed/inconclusive campaign carries an empty winner
        # plan, so there is nothing to apply — report APPLIED_NOTHING (not
        # SKIPPED) so the outcome is explicit and auditable. Dry-run and
        # non-empty-plan skips keep the SKIPPED status.
        raw_plan = state.get("knob_plan") or {}
        plan_knobs = (
            raw_plan.get("knobs", [])
            if isinstance(raw_plan, dict)
            else getattr(raw_plan, "knobs", []) or []
        )
        if not dry_run and not plan_knobs:
            live_result = {
                "status": "APPLIED_NOTHING",
                "reason": "no confirmed winner; nothing to apply",
                "applied_knobs": [],
                "persisted_static_knobs": [],
                "pending_restart_knobs": [],
            }
            progress(f"live apply: {live_result['reason']}")
            return Event(
                output=live_result,
                state={"live_result": live_result, "applied_knobs": []},
            )
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

    resolved_raw = state.get("apply_mode_resolved", None)
    if resolved_raw is None:
        resolved_raw = state.get("apply_mode", "live")
    mode = coerce_apply_mode(resolved_raw)
    knobs = [spec.model_dump() for spec in plan.knobs]

    pending_restart = [
        spec.model_dump() for spec in plan.knobs if requires_restart(spec)
    ]

    # Phase 1.5: refuse to mutate a live target that is not the database the
    # plan was staged against. The staging record is the attestation's
    # database_identity (recorded as staging_database_identity); only the
    # (engine, database) pair is compared because staging host/ports are
    # ephemeral.
    live_identity = _live_target_identity(cfg, str(state.get("db_type", "") or ""))
    staging_identity = _staging_identity_from_state(state)
    if not _staging_identity_matches(staging_identity, live_identity):
        reason = (
            "staging/live database mismatch: plan was staged against "
            f"{staging_identity or 'unknown'}, live target is {live_identity}; "
            "refusing to apply"
        )
        progress(f"live apply refused: {reason}")
        live_result = {
            "status": "FAILED",
            "reason": reason,
            "applied_knobs": [],
            "persisted_static_knobs": [],
            "pending_restart_knobs": pending_restart,
            "results": [],
            "staging_database_identity": staging_identity,
            "live_database_identity": live_identity,
        }
        return Event(
            output=live_result,
            state={
                "live_result": live_result,
                "applied_knobs": [],
                "result_status": TuningStatus.FAIL.value,
            },
        )

    route = str(state.get("preflight_route") or "").strip().lower()
    verdict_raw = state.get("preflight_verdict") or {}
    verdict_reason = (
        verdict_raw.get("reason", "") if isinstance(verdict_raw, dict) else ""
    )

    if route == "blocked":
        live_result = {
            "status": "FAILED",
            "reason": f"preflight blocked: {verdict_reason or 'apply gated'}; "
            "no production mutation",
            "applied_knobs": [],
            "persisted_static_knobs": [],
            "pending_restart_knobs": pending_restart,
            "manual_sql": [],
        }
        return Event(
            output=live_result,
            state={
                "live_result": live_result,
                "applied_knobs": [],
                "result_status": TuningStatus.FAIL.value,
            },
        )

    if mode == ApplyMode.MANUAL or route == "maintenance_assisted":
        if route == "maintenance_assisted" and mode != ApplyMode.MANUAL:
            reason = (
                "preflight maintenance_assisted: manual SQL emitted; "
                f"no auto restart ({verdict_reason})"
                if verdict_reason
                else "preflight maintenance_assisted: manual SQL emitted; "
                "no auto restart"
            )
        else:
            reason = "manual: manual SQL emitted; no auto restart"
        progress("manual: generating manual SQL (no production mutation)...")
        try:
            sql_results = apply_knobs(knobs, cfg, dry_run=True, mode=mode)
            manual_sql = [r.get("sql", "") for r in sql_results if r.get("sql")]
            live_result = {
                "status": "MANUAL_SQL",
                "reason": reason,
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
        failed = [r for r in results if r.get("status") == "failed"]
        skipped = [r for r in results if r.get("status") == "skipped"]
        restart_names = {
            spec.name for spec in plan.knobs if requires_restart(spec)
        }
        persisted_static = [r for r in applied if r.get("knob") in restart_names]
        persisted_names = sorted(
            str(r.get("knob")) for r in persisted_static if r.get("knob")
        )
        progress(
            f"applied live: {len(applied)}; persisted (pending restart): "
            f"{', '.join(persisted_names) or 'none'}"
        )
        # Phase 1.4: explicit apply outcome. An empty `applied` list is never
        # reported as a success: APPLIED (all), PARTIAL (some), APPLIED_NOTHING
        # (none — every-knob-failed, every-knob-skipped, or --apply-mode none).
        total = len(results)
        if applied and len(applied) == total:
            outcome = "APPLIED"
            reason = ""
        elif applied:
            outcome = "PARTIAL"
            reason = (
                f"partial apply: {len(applied)}/{total} knobs applied "
                f"({len(failed)} failed, {len(skipped)} skipped)"
            )
        elif not results:
            outcome = "APPLIED_NOTHING"
            reason = "no knobs in plan; nothing applied"
        elif failed and not skipped:
            outcome = "APPLIED_NOTHING"
            names = ", ".join(str(r.get("knob")) for r in failed[:3])
            reason = f"every knob failed ({len(failed)}/{total}): {names}"
        elif skipped and not failed:
            outcome = "APPLIED_NOTHING"
            if mode == ApplyMode.NONE:
                reason = (
                    f"apply mode is 'none': all {total} knobs skipped; "
                    "nothing applied"
                )
            else:
                reason = (
                    f"every knob skipped under {mode.value} apply mode "
                    f"({total}/{total}); nothing applied"
                )
        else:
            outcome = "APPLIED_NOTHING"
            reason = (
                f"nothing applied "
                f"({len(failed)} failed, {len(skipped)} skipped of {total})"
            )
        live_result = {
            "status": outcome,
            "reason": reason,
            "applied_knobs": applied,
            "persisted_static_knobs": persisted_static,
            "pending_restart_knobs": pending_restart,
            "results": results,
            "staging_database_identity": staging_identity,
            "live_database_identity": live_identity,
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
    elif live_result["status"] in ("APPLIED_NOTHING", "PARTIAL"):
        # Empty/partial applies never stay PASS: every-knob-failed is a FAIL,
        # every-knob-skipped / mode-none / partial is INCONCLUSIVE.
        res = [
            r
            for r in (live_result.get("results") or [])
            if isinstance(r, dict)
        ]
        any_failed = any(r.get("status") == "failed" for r in res)
        if live_result["status"] == "APPLIED_NOTHING" and any_failed:
            apply_state["result_status"] = TuningStatus.FAIL.value
        else:
            apply_state["result_status"] = TuningStatus.INCONCLUSIVE.value
    return Event(output=live_result, state=apply_state)


def finalize_node(ctx: Context, node_input: Any = None) -> Event:
    """Compose and persist the run manifest from session state."""
    state = ctx.state
    progress = make_progress_callback(
        state.get("log_file"), bool(state.get("verbose", False))
    )

    # Run-scoped snapshot cleanup belongs HERE (the terminal node), not in the
    # decision node: confirmation runs after decision and still needs the
    # prepared-dataset snapshot. Best-effort — a failed image removal never
    # fails the run.
    with contextlib.suppress(Exception):
        registry = SnapshotRegistry.from_state(state.get("snapshot_registry"))
        for image in registry.images():
            with contextlib.suppress(Exception):
                cleanup_snapshot_image(image)

    # Single source of truth: the shared builder owns db_image/errors/status
    # reconciliation (see tools.run_artifacts.build_run_manifest).
    manifest = build_run_manifest(state)
    progress(f"run complete: {manifest.status.value}")

    return Event(
        output=manifest.model_dump(),
        state={
            "run_manifest": manifest.model_dump(),
            "result_status": manifest.status.value,
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
    progress = make_progress_callback(
        ctx.state.get("log_file"), bool(ctx.state.get("verbose", False))
    )
    result = stage_nodes.compile_candidate(ctx, node_input)
    if isinstance(result, CompileRejection):
        ctx.route = "rejected"
        ctx.state["exp_name"] = result.design_name or "experiment"
        progress(f"compile {ctx.state['exp_name']}: rejected ({result.reason})")
        # Audit trail: the CompileRejection object travels to the controller,
        # but only this state projection survives it — persist the reason and
        # errors so manifest readers see why the proposal never ran.
        with contextlib.suppress(Exception):
            ctx.state["last_rejection"] = result.model_dump()
    else:
        ctx.route = "ok"
        ctx.state["exp_name"] = result.exp_name
        ctx.state["phase"] = result.phase
        ctx.state["n_knobs"] = len(result.valid_knobs)
        progress(
            f"compile {result.exp_name}: ok "
            f"({len(result.valid_knobs)} knobs, phase={result.phase})"
        )
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
    # Phase 4.3: canonical database name (compat mirrors handled in helper).
    database = get_database_name(state)
    benchmark_kind = str(state.get("screening_benchmark", "sysbench") or "sysbench")
    dry_run = bool(state.get("dry_run", False))
    measure_reps = get_measure_reps(state)
    measure_seconds = get_measure_seconds(state)
    measure_warmup_seconds = get_measure_warmup_seconds(state)
    # Phase 4.2: min_improvement_pct is written once by prepare_run;
    # screen/confirm edges read it from state (no local re-derivation).

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
            "repetitions": measure_reps,
            "measurement_seconds": measure_seconds,
            "warmup_seconds": measure_warmup_seconds,
        }
    )

    registry = SnapshotRegistry.from_state(state.get("snapshot_registry"))
    state["snapshot_registry"] = registry.to_state()

    def _validate(
        plan: KnobPlan,
        run_profile: Any,
        shared_baseline: Any,
        attempt: int,
        early_stop_min_reps: Any = None,
        baseline_only: bool = False,
    ) -> dict[str, Any]:
        result = validate_plan(
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
            # separately by live / manual.
            apply_mode=ApplyMode.MANUAL,
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
        # Phase 4.6: the live registry mutated above — sync its plain-data
        # view back so snapshot reuse survives across screens.
        with contextlib.suppress(Exception):
            state["snapshot_registry"] = registry.to_state()
        return result

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
    out = stage_nodes.confirmation_controller(ctx, node_input)
    # Audit trail: the decision node runs after this controller and only
    # reads session state, so the routing reason (gate_info/reason) must be
    # persisted here or it is invisible downstream.
    with contextlib.suppress(Exception):
        state = ctx.state
        state["last_controller"] = dict(out)
        # Phase 4.5: read-copy-reassign, never mutate the live list in place.
        history = list(state.get("controller_history") or [])
        history.append(dict(out))
        state["controller_history"] = history
    progress = make_progress_callback(
        ctx.state.get("log_file"), bool(ctx.state.get("verbose", False))
    )
    progress(
        f"controller: route={out.get('route')} "
        f"reason={out.get('reason', '')} "
        f"attempt={out.get('validation_attempt_count')}/{out.get('max_attempts')}"
    )
    return out


_DECISION_STATUS = {
    "apply_winner": TuningStatus.PASS.value,
    "keep_best": TuningStatus.PASS.value,
    "inconclusive": TuningStatus.INCONCLUSIVE.value,
    "fail": TuningStatus.FAIL.value,
}


def decision_node(ctx: Context) -> TerminalDecision:
    """Pick the terminal outcome and bridge it into apply/finalize state keys."""
    term = stage_nodes.decision(ctx)
    # Phase 1.7 belt-and-braces: a non-winning plan must never bridge to an
    # apply — downgrade a stray keep_best to an empty inconclusive decision.
    if term.decision == "keep_best":
        summary = dict(term.summary or {})
        reasons = list(summary.get("reasons", []) or [])
        note = "unconfirmed best withheld: nothing applied"
        if note not in reasons:
            reasons.append(note)
        summary["reasons"] = reasons
        summary["status"] = "INCONCLUSIVE"
        summary["plan_hash"] = ""
        term = TerminalDecision(
            decision="inconclusive", winner_plan={}, summary=summary
        )
    return _bridge_terminal_to_state(ctx, term)


def _bridge_terminal_to_state(ctx: Context, term: TerminalDecision) -> TerminalDecision:
    """Mirror a terminal decision into the apply/finalize state keys.

    Shared by ``decision_node`` and ``confirm_winner_node`` so a changed
    winner (post-confirmation) is reflected in ``knob_plan``,
    ``result_status`` and the staging-issue audit trail.
    """
    state = ctx.state
    status = _DECISION_STATUS.get(term.decision, TuningStatus.INCONCLUSIVE.value)
    winner = dict(term.winner_plan or {})
    summary = term.summary or {}
    try:
        plan_hash = KnobPlan.model_validate(winner).plan_hash() if winner else ""
    except ValueError:
        plan_hash = ""
    state["knob_plan"] = winner if winner else {"knobs": []}
    state["knob_plan_hash"] = plan_hash or str(summary.get("plan_hash", "") or "")
    state["result_status"] = status
    state["staging_validated"] = status == TuningStatus.PASS.value
    try:
        issue_attempt = int(state.get("validation_attempt_count") or 0)
    except (TypeError, ValueError):
        issue_attempt = 0
    issue_knobs = [
        str(spec.get("name"))
        for spec in (winner.get("knobs", []) if isinstance(winner, dict) else [])
        if isinstance(spec, dict) and spec.get("name")
    ]
    state["staging_issues"] = stage_nodes.structure_staging_issues(
        summary.get("reasons", []) or [],
        attempt=issue_attempt,
        knob_names=issue_knobs,
    )
    state["validation_attempts"] = list(state.get("experiment_history") or [])
    state["candidate_archive"] = summary.get("archive", {}) or {}
    state["improvement_confident"] = bool(
        summary.get("improvement_confident", False)
    )
    if isinstance(summary.get("confirmation"), dict):
        state["confirmation"] = summary["confirmation"]
    # NOTE: snapshot images are NOT cleaned up here. The confirmation node runs
    # AFTER the decision node and reuses the prepared-dataset snapshot to
    # re-measure the winner; deleting it here would force a silent fallback to
    # an empty stock image (see finalize_node, which owns run-scoped cleanup).
    return term


def confirm_winner_node(ctx: Context, node_input: TerminalDecision) -> TerminalDecision:
    """Re-measure the terminal winner with fresh reps (winner's-curse guard).

    Runs after the loop (cannot affect the quota or attempt cap). Builds a
    fresh validate_fn/run_profile against the same shared baseline, re-screens
    the winner, and promotes the runner-up if the fresh verdict no longer
    clears the bar. Re-bridges state so a changed winner flows to apply.
    """
    validate_fn, run_profile = _screen_runtime(ctx)
    term = stage_nodes.confirm_winner(
        ctx, node_input, validate_fn=validate_fn, run_profile=run_profile
    )
    return _bridge_terminal_to_state(ctx, term)


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
        # Phase 4.5: read-copy-reassign — never mutate the live list in place.
        issues = list(state.get("staging_issues") or [])
        try:
            issue_attempt = int(state.get("validation_attempt_count") or 0)
        except (TypeError, ValueError):
            issue_attempt = 0
        structured = stage_nodes.structure_staging_issues(
            [f"preflight blocked: {verdict.reason}"],
            attempt=issue_attempt,
            knob_names=None,
        )
        for entry in structured:
            if entry not in issues:
                issues.append(entry)
        state["staging_issues"] = issues
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
    confirm_node = FunctionNode(
        func=confirm_winner_node, name="confirm_winner_node", rerun_on_resume=True
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
            "decide/confirm/preflight/apply workflow."
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
            (decide_node, confirm_node),
            (confirm_node, preflight_node),
            (preflight_node, apply_live_node),
            (apply_live_node, finalize_node),
        ],
    )
