"""Terminal stages: decision, production preflight, and run preparation.

``decision`` picks the terminal outcome, ``production_preflight`` routes it
(auto / maintenance-assisted / blocked, never mutating production), and
``prepare_run`` resolves run configuration into state. Depends on
:mod:`_common` and :mod:`accounting`.
"""

from __future__ import annotations

from typing import Any

from src.knob_tuner.contracts import (
    ApplyMode,
    DEFAULT_MIN_IMPROVEMENT_PCT,
    KnobPlan,
    ResourceBudget,
    get_database_name,
    get_db_config_path,
    get_early_stop_min_reps,
    get_max_attempts,
    get_max_set_knobs,
    get_measure_reps,
    get_measure_seconds,
    get_measure_warmup_seconds,
    get_min_improvement_pct,
    get_success_candidates,
)
from src.knob_tuner.stages.models import (
    CompiledPlan,
    PreflightVerdict,
    TerminalDecision,
)
from src.knob_tuner.tools.knob_scope import requires_restart
from src.knob_tuner.tools.knobs import coerce_apply_mode, coerce_profile
from src.knob_tuner.stages.nodes._common import _jsonable, _state


# ---------------------------------------------------------------------------
# 6. decision
# ---------------------------------------------------------------------------


def _row_float(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _row_plan_name(row: dict[str, Any]) -> Any:
    plan = row.get("plan")
    if plan is not None:
        return plan
    result = row.get("result")
    if isinstance(result, dict):
        return result.get("plan")
    return getattr(result, "plan", None)


def _plan_dump(plan: Any) -> dict[str, Any]:
    if plan is None:
        return {}
    if isinstance(plan, dict):
        return plan
    if isinstance(plan, KnobPlan):
        return plan.model_dump()
    if hasattr(plan, "model_dump"):
        try:
            dumped = plan.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    return {}


def _cand_paired(candidate: Any) -> Any:
    if isinstance(candidate, dict):
        res = candidate.get("result")
        if isinstance(res, dict):
            return res.get("paired")
        return getattr(res, "paired", None)
    return getattr(getattr(candidate, "result", None), "paired", None)


def decision(
    ctx: Any,
    all_rows: list | None = None,
    candidates: list | None = None,
    baseline_tps: list | None = None,
    min_improvement_pct: float | None = None,
    durability_profile: str | None = None,
) -> TerminalDecision:
    """Pick the terminal outcome from collected screen rows.

    Ports the strong-win / pick-winner / INCONCLUSIVE / FAIL logic from
    ``workflow`` (lines 869-974) plus the archive payload (returned as data —
    no file writes). Never returns ``None``.

    Phase 1.7 / compounding quota: a confirmed PASS applies
    (``apply_winner``) only when its LCB exceeds ``min_improvement_pct`` — the
    best such row (highest mean, tie-broken by LCB) wins. This is the SAME
    definition the campaign's success-candidate quota uses, so "winner" has
    one meaning in the codebase. Only failures, unconfirmed rows, rows whose
    LCB misses the bar, or futility (no qualifying PASS) withhold, yielding
    ``inconclusive``/``fail``.
    """
    state = _state(ctx)
    try:
        rows: list[dict[str, Any]] = (
            list(all_rows)
            if all_rows is not None
            # Phase 4.3: all_rows is canonical (screen_rows was never written).
            else list(state.get("all_rows") or [])
        )
        cands: list[dict[str, Any]] = (
            list(candidates)
            if candidates is not None
            else list(state.get("candidates") or [])
        )
        if baseline_tps is None:
            baseline_tps = state.get("baseline_tps") or []
        if min_improvement_pct is None:
            # Phase 4.2: plain .get WITHOUT `or` — explicit 0.0 honored.
            min_improvement_pct = get_min_improvement_pct(state)
        if durability_profile is None:
            durability_profile = (
                str(state.get("durability_profile", "strict") or "strict").strip().lower()
            )
        experiment_history = state.get("experiment_history") or []
        try:
            experiments_run = int(state.get("experiments_run") or 0)
        except (TypeError, ValueError):
            experiments_run = 0
        if experiments_run <= 0:
            experiments_run = len(rows)
        try:
            validation_attempts = int(state.get("validation_attempt_count") or 0)
        except (TypeError, ValueError):
            validation_attempts = 0
        dataset = state.get("screen_dataset") or state.get("dataset") or {}
        single_fidelity = {
            "enabled": True,
            "measure_reps": get_measure_reps(state),
            "measure_seconds": get_measure_seconds(state),
            "measure_warmup_seconds": get_measure_warmup_seconds(state),
            "early_stop_min_reps": get_early_stop_min_reps(state),
        }

        winner_row: dict[str, Any] | None = None
        outcome = "fail"
        # Phase 1.7 / compounding quota: a confirmed PASS applies only when its
        # LCB clears min_improvement_pct — the SAME definition the campaign's
        # success-candidate quota uses, so "winner" has one meaning. Pick the
        # best such row — highest mean, tie-broken by LCB — not the first seen.
        confirmed_pass_rows = [
            row
            for row in rows
            if str(row.get("status", "")).upper() == "PASS"
            and bool(row.get("confirmed", False))
            and _row_float(row, "lcb_pct") > float(min_improvement_pct)
        ]
        if confirmed_pass_rows:
            winner_row = max(
                confirmed_pass_rows,
                key=lambda row: (
                    _row_float(row, "mean_delta_pct"),
                    _row_float(row, "lcb_pct"),
                ),
            )
            outcome = "apply_winner"
        withheld_best = False
        if winner_row is None:
            # No confirmed PASS row exists (otherwise it would have won), so
            # any confirmed row here is a non-PASS best: futility/unconfirmed
            # campaigns apply nothing.
            withheld_best = any(bool(r.get("confirmed")) for r in rows)
        winner_plan_raw: Any = None
        winner_stats: dict[str, float] = {}
        winner_paired: Any = None
        if winner_row is not None:
            winner_plan_raw = _row_plan_name(winner_row)
            if winner_plan_raw is None:
                winner_hash_lookup = winner_row.get("plan_hash")
                if winner_hash_lookup:
                    for cand in cands:
                        if not isinstance(cand, dict):
                            continue
                        cand_hash = cand.get("plan_hash")
                        if cand_hash is None:
                            try:
                                cand_dump = _plan_dump(cand.get("plan"))
                                cand_hash = (
                                    KnobPlan.model_validate(cand_dump).plan_hash()
                                    if cand_dump
                                    else None
                                )
                            except Exception:
                                cand_hash = None
                        if cand_hash is not None and cand_hash == winner_hash_lookup:
                            winner_plan_raw = cand.get("plan")
                            break
            for key in ("mean_delta_pct", "lcb_pct", "ucb_pct", "df"):
                try:
                    winner_stats[key] = float(winner_row.get(key, 0.0) or 0.0)
                except (TypeError, ValueError):
                    winner_stats[key] = 0.0
            winner_paired = winner_row.get("paired")
        if winner_row is None:
            # Truthiness, not `is not None`: an empty/placeholder paired
            # mapping is no measurement, so a hard FAIL with no real samples
            # stays "fail" instead of degrading into "inconclusive".
            ever_paired = bool(baseline_tps) or any(
                bool(r.get("paired")) for r in rows
            )
            outcome = "inconclusive" if ever_paired else "fail"

        winner_dump = _plan_dump(winner_plan_raw)
        try:
            winner_hash = (
                KnobPlan.model_validate(winner_dump).plan_hash()
                if winner_dump
                else ""
            )
        except Exception:
            winner_hash = ""
        measurement_problem = bool(
            cands and any(_cand_paired(c) is None for c in cands)
        )
        archive_payload = {
            "dataset": _jsonable(dataset),
            "durability_profile": durability_profile,
            "single_fidelity": single_fidelity,
            "min_improvement_pct": float(min_improvement_pct),
            "candidates": [
                {
                    k: _jsonable(v)
                    for k, v in r.items()
                    if k not in ("plan", "result")
                }
                for r in rows
            ],
            "winner": winner_hash,
            "measurement_problem": measurement_problem,
            "winner_improvement_confident": bool(
                winner_row and winner_row.get("improvement_confident")
            ),
            "experiment_history": _jsonable(experiment_history),
            "experiments_run": experiments_run,
            "success_candidates": get_success_candidates(state),
            "winners_found": len(confirmed_pass_rows),
            "success_knobs": _jsonable(state.get("success_knobs") or ""),
            "confirmation": _jsonable(state.get("confirmation") or {}),
        }
        reasons = [str(r) for row in rows for r in (row.get("reasons") or [])]
        # Audit trail: compile rejections never produce screen rows, so row
        # reasons alone would silently drop them. Append the rejected history
        # (deduped, order-preserving) instead of preferring row reasons only.
        for extra in list(state.get("rejected_history") or []) + list(
            state.get("last_failure") or []
        ):
            text = str(extra)
            if text and text not in reasons:
                reasons.append(text)
        # Surface the controller's routing reason (gate_info/reason) in the
        # decision output: the controller runs before this node and its
        # verdict is otherwise invisible downstream.
        controller = state.get("last_controller") or {}
        if not isinstance(controller, dict):
            controller = {}
        controller_route = str(controller.get("route", "") or "")
        controller_reason = str(controller.get("reason", "") or "")
        if withheld_best:
            reasons = list(reasons) + [
                "unconfirmed best withheld: no confirmed winner above "
                "threshold; nothing applied"
            ]
        if controller_route or controller_reason:
            note = f"controller: {controller_route or '?'}".rstrip()
            if controller_reason:
                note += f" ({controller_reason})"
            gate = str(controller.get("gate", "") or "")
            if gate:
                note += f" [gate={gate}]"
            if note not in reasons:
                reasons.append(note)
        summary = {
            "status": outcome.upper(),
            "plan_hash": winner_hash,
            "attempt_count": validation_attempts or experiments_run,
            "reasons": reasons,
            "paired": _jsonable(winner_paired),
            "improvement_confident": archive_payload["winner_improvement_confident"],
            "dataset": _jsonable(dataset),
            "durability_profile": durability_profile,
            "stats": winner_stats,
            "archive": archive_payload,
        }
        if controller:
            summary["controller_route"] = controller_route
            if controller_reason:
                summary["controller_reason"] = controller_reason
            for key in (
                "gate",
                "diag_confidence",
                "stat_p_win",
                "stop_reason",
                "thresholds",
                "rejection_reason",
                "rejection_errors",
                "design_name",
            ):
                if controller.get(key) is not None:
                    summary[f"controller_{key}"] = _jsonable(controller.get(key))
        return TerminalDecision(
            decision=outcome, winner_plan=winner_dump, summary=summary
        )
    except Exception as exc:
        return TerminalDecision(
            decision="fail",
            winner_plan={},
            summary={"status": "FAIL", "reasons": [f"decision error: {exc}"], "archive": {}},
        )


# ---------------------------------------------------------------------------
# 6b. confirm_winner (anti-winner's-curse re-measurement)
# ---------------------------------------------------------------------------


def _qualifies(row: dict[str, Any], min_pct: float) -> bool:
    """Whether a row is a confirmed PASS clearing the LCB bar."""
    try:
        return (
            str(row.get("status", "")).upper() == "PASS"
            and bool(row.get("confirmed", False))
            and _row_float(row, "lcb_pct") > min_pct
        )
    except Exception:
        return False


def _candidate_pool(state: Any, min_pct: float) -> list[dict[str, Any]]:
    """Rank the confirming candidate pool: clearing rows first, by mean/lcb.

    When the winner quota target is > 1, only LCB-clearing rows are eligible;
    otherwise fall back to confirmed PASS rows (today's pool) so a
    single-winner campaign still confirms the row the decision picked.
    """
    rows = [r for r in (state.get("all_rows") or []) if isinstance(r, dict)]
    try:
        target = get_success_candidates(state)
    except Exception:
        target = 1
    pool = [r for r in rows if _qualifies(r, min_pct)]
    if not pool and target <= 1:
        pool = [
            r
            for r in rows
            if str(r.get("status", "")).upper() == "PASS"
            and bool(r.get("confirmed", False))
        ]
    pool.sort(
        key=lambda r: (_row_float(r, "mean_delta_pct"), _row_float(r, "lcb_pct")),
        reverse=True,
    )
    return pool


def confirm_winner(
    ctx: Any,
    node_input: Any,
    validate_fn: Any = None,
    run_profile: Any = None,
) -> TerminalDecision:
    """Re-measure terminal candidates with fresh reps (winner's-curse guard).

    Runs AFTER the loop, so it cannot affect the quota or the attempt cap. It
    walks the ranked candidate pool best-first — the chosen winner, then the
    runner-up, then 3rd, 4th, ... — re-screening each against the SAME shared
    baseline with fresh reps, and stops at the FIRST one whose fresh verdict
    still clears the bar. That one is applied. If NO candidate survives the
    re-measurement, nothing is applied (inconclusive). Always returns a
    :class:`TerminalDecision`.
    """
    state = _state(ctx)
    try:
        from src.knob_tuner.stages.nodes.stats_coercion import screen_candidate

        term = node_input
        if not isinstance(term, TerminalDecision):
            term = TerminalDecision.model_validate(_jsonable(node_input))
        summary = dict(term.summary or {})
        min_pct = float(min_improvement_pct_from_state(state))
        confirmation: dict[str, Any] = {
            "attempted": 0,
            "plan_hash": "",
            "verdict": "skipped",
            "promoted": False,
        }
        # Nothing to confirm unless the decision chose a winner.
        if term.decision != "apply_winner" or not term.winner_plan:
            summary["confirmation"] = confirmation
            return TerminalDecision(
                decision=term.decision, winner_plan=term.winner_plan, summary=summary
            )
        if validate_fn is None:
            validate_fn = state.get("validate_fn")
        if run_profile is None:
            run_profile = state.get("run_profile")
        pool = _candidate_pool(state, min_pct)
        if not pool:
            confirmation["verdict"] = "no_candidate"
            summary["confirmation"] = confirmation
            return TerminalDecision(
                decision="inconclusive", winner_plan={}, summary=_with_note(
                    summary,
                    "confirmation: no confirmable candidate in pool; nothing applied",
                )
            )
        tried = 0
        promoted = False
        last_verdict = "fail"
        chosen: dict[str, Any] | None = None
        last_hash = ""
        for row in pool:
            plan_dump = _plan_dump(_row_plan_name(row))
            if not plan_dump or not plan_dump.get("knobs"):
                continue
            try:
                last_hash = KnobPlan.model_validate(plan_dump).plan_hash()
            except Exception:
                last_hash = ""
            compiled = CompiledPlan(
                plan=plan_dump,
                exp_name="confirm_winner",
                phase=str(row.get("phase", "refinement") or "refinement"),
            )
            verdict = screen_candidate(
                ctx, compiled, validate_fn=validate_fn, run_profile=run_profile
            )
            tried += 1
            v_status = getattr(verdict, "status", "FAIL")
            v_lcb = float(getattr(verdict, "lcb_pct", 0.0) or 0.0)
            v_conf = bool(getattr(verdict, "confirmed", False))
            if str(v_status).upper() == "PASS" and v_conf and v_lcb > min_pct:
                last_verdict = "pass"
                chosen = row
                promoted = tried > 1
                break
            last_verdict = "fail"
        confirmation.update(
            {"attempted": tried, "plan_hash": last_hash, "verdict": last_verdict,
             "promoted": promoted}
        )
        if chosen is None:
            summary["confirmation"] = confirmation
            return TerminalDecision(
                decision="inconclusive",
                winner_plan={},
                summary=_with_note(
                    summary,
                    f"confirmation: no candidate cleared the bar after {tried} "
                    "re-measurement(s); nothing applied (inconclusive)",
                ),
            )
        winner_dump = _plan_dump(_row_plan_name(chosen))
        summary["confirmation"] = confirmation
        return TerminalDecision(
            decision="apply_winner", winner_plan=winner_dump, summary=summary
        )
    except Exception as exc:
        return TerminalDecision(
            decision="inconclusive",
            winner_plan={},
            summary={"status": "INCONCLUSIVE",
                     "reasons": [f"confirmation error: {exc}"],
                     "confirmation": {"verdict": "error"}, "archive": {}},
        )


def _with_note(summary: dict[str, Any], note: str) -> dict[str, Any]:
    reasons = list(summary.get("reasons", []) or [])
    if note not in reasons:
        reasons.append(note)
    summary = dict(summary)
    summary["reasons"] = reasons
    return summary


def min_improvement_pct_from_state(state: Any) -> float:
    """Read the win-gate pct from state (never throws)."""
    try:
        return float(get_min_improvement_pct(state))
    except Exception:
        return DEFAULT_MIN_IMPROVEMENT_PCT


# ---------------------------------------------------------------------------
# 7. production_preflight
# ---------------------------------------------------------------------------

def _extract_winner_plan(node_input: Any, state: dict[str, Any]) -> dict[str, Any]:
    if isinstance(node_input, TerminalDecision):
        return dict(node_input.winner_plan or {})
    if isinstance(node_input, dict):
        raw = node_input.get("winner_plan", node_input.get("plan", node_input))
        if isinstance(raw, dict) and "knobs" in raw:
            return raw
        if isinstance(raw, dict):
            return raw
    if hasattr(node_input, "model_dump"):
        try:
            dumped = node_input.model_dump()
            if isinstance(dumped, dict) and isinstance(dumped.get("winner_plan"), dict):
                return dumped["winner_plan"]
        except Exception:
            pass
    raw_state = state.get("knob_plan") or {}
    if isinstance(raw_state, dict):
        return raw_state
    if hasattr(raw_state, "model_dump"):
        try:
            return raw_state.model_dump()
        except Exception:
            return {}
    return {}


def production_preflight(
    ctx: Any,
    node_input: Any,
    apply_mode: str | None = None,
    durability_profile: str | None = None,
) -> PreflightVerdict:
    """Route a terminal decision to auto / maintenance-assisted / blocked."""
    state = _state(ctx)
    try:
        if apply_mode is None:
            # Single authority: prefer the canonical value resolved once in
            # prepare_run; fall back to the raw user input for older states.
            resolved = state.get("apply_mode_resolved", None)
            raw = state.get("apply_mode", "live") or "live"
            apply_mode = resolved if resolved is not None else raw
        mode = coerce_apply_mode(apply_mode)
        if durability_profile is None:
            durability_profile = (
                str(state.get("durability_profile", "strict") or "strict").strip().lower()
            )
        plan = _extract_winner_plan(node_input, state)
        knobs = plan.get("knobs", []) if isinstance(plan, dict) else []
        if not knobs:
            return PreflightVerdict(route="blocked", reason="empty plan: nothing to apply")
        if mode == ApplyMode.MANUAL:
            return PreflightVerdict(
                route="maintenance_assisted",
                reason=f"apply_mode={mode.value}: manual SQL only, no production mutation",
            )
        restart_names = sorted(
            {
                str(k.get("name", ""))
                for k in knobs
                if isinstance(k, dict) and requires_restart(k)
            }
            - {""}
        )
        if restart_names:
            return PreflightVerdict(
                route="auto",
                reason=(
                    "restart-required knobs "
                    f"({', '.join(restart_names)}) persisted under live; "
                    "activated on operator restart (tuner never restarts)"
                ),
            )
        return PreflightVerdict(
            route="auto",
            reason=f"all {len(knobs)} knobs dynamic under durability={durability_profile}",
        )
    except Exception as exc:
        return PreflightVerdict(route="blocked", reason=f"preflight error: {exc}")


# ---------------------------------------------------------------------------
# 8. prepare_run
# ---------------------------------------------------------------------------


def prepare_run(ctx: Any) -> dict[str, Any]:
    """Resolve run configuration from state (read-only; no benchmarking).

    Ports the read-only resolution part of the legacy closed-loop tuner (budget, profile, db
    config slot, snapshot/baseline slots, loop counters) and initializes loop
    counters idempotently. Never returns ``None``.
    """
    state = _state(ctx)
    try:
        payload: dict[str, Any] = {}
        try:
            raw_budget = state.get("resource_budget") or {}
            if isinstance(raw_budget, ResourceBudget):
                budget = raw_budget
            else:
                budget = ResourceBudget.model_validate(dict(raw_budget))
            payload["resource_budget"] = budget.model_dump()
        except Exception as exc:
            payload["resource_budget"] = {}
            payload["budget_error"] = f"invalid resource_budget: {exc}"

        profile = coerce_profile(state.get("sysbench_profile"))
        payload["sysbench_profile"] = profile.model_dump()
        # Phase 4.1/4.3: canonical loop cap, persisted for downstream readers.
        max_attempts = get_max_attempts(state)
        payload["max_attempts"] = max_attempts
        # Compounding-DOE quota target, persisted for the controller.
        success_candidates = get_success_candidates(state)
        payload["success_candidates"] = success_candidates
        payload["dry_run"] = bool(state.get("dry_run", False))
        payload["run_id"] = state.get("run_id", "") or ""
        payload["run_dir"] = state.get("run_dir", "") or ""
        payload["db_type"] = state.get("db_type", "postgres") or "postgres"
        payload["db_version"] = state.get("db_version")
        # Phase 4.3: canonical database name (mirrors deleted in R3).
        payload["database"] = get_database_name(state)
        payload["benchmark_kind"] = str(
            state.get("screening_benchmark", "sysbench") or "sysbench"
        )
        payload["workload_hint"] = str(state.get("workload_hint", "") or "")
        # Single source for the timing defaults + clamps (contracts is canonical).
        payload["measure_reps"] = get_measure_reps(state)
        payload["measure_seconds"] = get_measure_seconds(state)
        payload["measure_warmup_seconds"] = get_measure_warmup_seconds(state)
        payload["early_stop_min_reps"] = get_early_stop_min_reps(state)
        # Phase 4.1: canonical knob cap.
        max_set_knobs = get_max_set_knobs(state)
        payload["max_set_knobs"] = max_set_knobs
        try:
            payload["min_improvement_pct"] = float(
                getattr(profile, "min_improvement_pct", DEFAULT_MIN_IMPROVEMENT_PCT)
            )
        except (TypeError, ValueError):
            payload["min_improvement_pct"] = DEFAULT_MIN_IMPROVEMENT_PCT
        # Phase 4.2: prepare_run is the ONE writer of min_improvement_pct —
        # downstream (screen/compile/controller/decision edges) read it from
        # state instead of each deriving their own copy.
        state["min_improvement_pct"] = payload["min_improvement_pct"]
        state["max_set_knobs"] = max_set_knobs
        # R3: canonical-only (mirrors db_name/dbname/config_path/attempt deleted).
        state["database"] = payload["database"]
        config_path = get_db_config_path(state)
        if config_path:
            state["db_config_path"] = config_path
        payload["durability_profile"] = "strict"
        state["durability_profile"] = "strict"
        # Single authority for apply mode: resolve the raw user input once
        # here via coerce_apply_mode (the only string->mode mapping).
        # state["apply_mode"] keeps the raw input; the canonical value lives in
        # state["apply_mode_resolved"] (+ payload mirror) for downstream nodes.
        resolved_mode = coerce_apply_mode(state.get("apply_mode", "live"))
        state["apply_mode_resolved"] = resolved_mode.value
        payload["apply_mode_resolved"] = resolved_mode.value
        # Unconfirmed plans are never applied (escape hatch removed).
        # Snapshot/baseline slots (reserved keys, no side effects here).
        payload["snapshot_slot"] = state.get("snapshot_slot", "run-snapshot")
        payload["baseline_slot"] = state.get("baseline_slot", "shared-baseline")
        payload["db_config_slot"] = bool(
            state.get("db_config")
            or state.get("db_config_path")
        )

        state.setdefault("validation_attempt_count", 0)
        state.setdefault("experiments_run", 0)
        state.setdefault("experiment_history", [])
        state.setdefault("rejected_history", [])
        state.setdefault("last_failure", [])
        state.setdefault("all_rows", [])
        state.setdefault("candidates", [])
        state["run_config"] = _jsonable(payload)
        # Belt-and-braces: the controller reads its cap from state, so persist
        # the resolved value explicitly (not just inside the run_config payload).
        state["max_attempts"] = max_attempts
        state["success_candidates"] = success_candidates
        return payload
    except Exception as exc:
        return {"error": f"prepare_run failed: {exc}"}
