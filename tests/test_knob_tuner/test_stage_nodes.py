"""Wave 2b stage-node tests: fake ctx objects, no ADK runtime, no DB."""

import json
import os

from src.knob_tuner.contracts import KnobPlan
from src.knob_tuner.stages import nodes
from src.knob_tuner.stages.memory_guard import (
    _clamp_memory_knobs,
    parse_mem_value_to_bytes,
)
from src.knob_tuner.stages.models import (
    CompiledPlan,
    CompileRejection,
    DiagnosisOutput,
    PreflightVerdict,
    ScreenVerdict,
    TerminalDecision,
)
from src.knob_tuner.sub_agents.db_inspector.models import DbInspectorOutput
from tests.test_knob_tuner.conftest import FakeCtx


def _inv_state(**over):
    state = {
        "knobs_info": [
            {
                "name": "work_mem",
                "current_value": "4MB",
                "unit": "kB",
                "vartype": "integer",
                "context": "user",
                "enumvals": [],
            },
            {
                "name": "shared_buffers",
                "current_value": "128MB",
                "unit": "8kB",
                "vartype": "integer",
                "context": "postmaster",
                "enumvals": [],
            },
            {
                "name": "synchronous_commit",
                "current_value": "on",
                "unit": "",
                "vartype": "enum",
                "context": "sighup",
                "enumvals": ["on", "off"],
            },
        ],
        "resource_budget": {"cpu_cores": 4, "memory_gb": 8.0},
        "durability_profile": "strict",
        "max_set_knobs": 20,
    }
    state.update(over)
    return state


def _proposal(name="exp-1", phase="screen", levels=None):
    return {
        "name": name,
        "phase": phase,
        "levels": levels
        if levels is not None
        else [{"knob": "work_mem", "value": "64MB"}],
    }


# --- compile accept/reject ---


def test_compile_accept():
    ctx = FakeCtx(_inv_state())
    out = nodes.compile_candidate(ctx, _proposal())
    assert isinstance(out, CompiledPlan)
    assert out.exp_name == "exp-1" and out.phase == "screen"
    assert "work_mem" in out.valid_knobs
    assert any(k["name"] == "work_mem" for k in out.plan["knobs"])


def test_compile_aliases_name_and_recommended_value():
    ctx = FakeCtx(_inv_state())
    out = nodes.compile_candidate(
        ctx,
        {
            "name": "e2",
            "phase": "REFINEMENT",
            "levels": [{"name": "work_mem", "recommended_value": "64MB"}],
        },
    )
    assert isinstance(out, CompiledPlan)
    assert out.phase == "refinement"


def test_compile_reject_oversize_cap():
    levels = [{"knob": f"k{i}", "value": "1"} for i in range(21)]
    big_inv = {
        "knobs_info": [
            {"name": f"k{i}", "current_value": "0", "context": "user", "enumvals": []}
            for i in range(21)
        ]
    }
    ctx = FakeCtx(_inv_state(**big_inv))
    out = nodes.compile_candidate(ctx, _proposal(levels=levels))
    assert isinstance(out, CompileRejection)
    assert "cap" in out.reason


def test_compile_reject_unknown_knob():
    ctx = FakeCtx(_inv_state())
    out = nodes.compile_candidate(ctx, _proposal(levels=[{"knob": "nope", "value": "1"}]))
    assert isinstance(out, CompileRejection)
    assert any("not in available knob inventory" in e for e in out.errors)


def test_compile_reject_noop():
    ctx = FakeCtx(_inv_state())
    out = nodes.compile_candidate(
        ctx, _proposal(levels=[{"knob": "work_mem", "value": "4MB"}])
    )
    assert isinstance(out, CompileRejection)
    assert any("no-op" in e for e in out.errors)


def test_compile_reject_bad_phase():
    ctx = FakeCtx(_inv_state())
    out = nodes.compile_candidate(ctx, _proposal(phase="bogus"))
    assert isinstance(out, CompileRejection)
    assert "unknown phase" in out.reason


def test_compile_reject_empty_levels():
    ctx = FakeCtx(_inv_state())
    out = nodes.compile_candidate(ctx, _proposal(levels=[]))
    assert isinstance(out, CompileRejection)
    assert "empty levels" in out.reason


def test_compile_reject_durability_strict():
    ctx = FakeCtx(_inv_state())
    out = nodes.compile_candidate(
        ctx, _proposal(levels=[{"knob": "synchronous_commit", "value": "off"}])
    )
    assert isinstance(out, CompileRejection)
    assert any("durability" in e for e in out.errors)


def test_compile_never_none_on_garbage():
    ctx = FakeCtx({})
    out = nodes.compile_candidate(ctx, None)
    assert isinstance(out, CompileRejection)


# --- memory guard: PG 40% rule ---


def test_memory_guard_pg_40pct_rule():
    recs = [{"name": "shared_buffers", "value": "100GB"}]
    clamped = _clamp_memory_knobs(recs, 2.0)
    assert clamped[0]["value"] != "100GB"
    limit = 0.40 * 2.0 * 1024**3
    assert parse_mem_value_to_bytes(clamped[0]["value"]) <= limit


def test_compile_sources_memory_from_resource_budget():
    # 1GB budget -> 40% cap = 0.4GB; 100GB must be clamped down.
    ctx = FakeCtx(_inv_state(resource_budget={"cpu_cores": 2, "memory_gb": 1.0}))
    out = nodes.compile_candidate(
        ctx, _proposal(levels=[{"knob": "shared_buffers", "value": "100GB"}])
    )
    assert isinstance(out, CompiledPlan)
    val = next(k["value"] for k in out.plan["knobs"] if k["name"] == "shared_buffers")
    assert parse_mem_value_to_bytes(val) <= 0.40 * 1.0 * 1024**3


# --- controller is a pure router (accounting lives in screen/compile) ---


def _routed_state(**over):
    state = {
        "max_attempts": 3,
        "validation_attempt_count": 1,
        "min_improvement_pct": 2.0,
        "success_candidates": 1,
        "experiment_history": [
            {
                "name": "e1",
                "phase": "screen",
                "n_knobs": 1,
                "mean_delta_pct": -1.0,
                "lcb_pct": -2.0,
                "status": "FAIL",
                "confirmed": False,
            }
        ],
        "rejected_history": ["slow"],
        "last_screen_row": {
            "status": "FAIL",
            "mean_delta_pct": -1.0,
            "lcb_pct": -2.0,
            "reasons": ["slow"],
        },
    }
    state.update(over)
    return state


def test_controller_retry_without_recount():
    ctx = FakeCtx(_routed_state())
    out = nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="drop_knob", targets=["work_mem"], rationale="r", confidence=0.7),
    )
    assert out["route"] == "retry" and ctx.route == "retry"
    assert out["validation_attempt_count"] == 1 and ctx.state["validation_attempt_count"] == 1
    assert len(ctx.state["experiment_history"]) == 1  # no second row
    assert len(ctx.state["diagnosis_history"]) == 1  # bookkeeping only
    assert out["status"] == "diagnosed:drop_knob"


def test_controller_done_at_budget_with_attempt_cap_reason():
    ctx = FakeCtx(_routed_state(max_attempts=2,
                                validation_attempt_count=2))
    out = nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="drop_knob", targets=["work_mem"], rationale="r", confidence=0.7),
    )
    assert out["route"] == "done" and ctx.route == "done"
    assert out["validation_attempt_count"] == 2
    assert out.get("reason") == "attempt_cap"


def test_controller_diagnosis_stop_done():
    ctx = FakeCtx(_routed_state())
    out = nodes.confirmation_controller(
        ctx, DiagnosisOutput(correction="stop", targets=[], rationale="exhausted", confidence=0.8)
    )
    assert out["route"] == "done" and ctx.route == "done"
    assert out.get("reason") == "stopped_by_diagnosis"


def test_controller_confident_win_backstop_without_stop():
    ctx = FakeCtx(
        _routed_state(
            last_screen_row={
                "status": "PASS",
                "mean_delta_pct": 5.0,
                "lcb_pct": 3.0,
                "reasons": [],
            },
            experiment_history=[
                {
                    "name": "e2",
                    "phase": "screen",
                    "n_knobs": 1,
                    "mean_delta_pct": 5.0,
                    "lcb_pct": 3.0,
                    "status": "PASS",
                    "confirmed": True,
                }
            ],
        )
    )
    out = nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="drop_knob", targets=["work_mem"], rationale="next", confidence=0.6),
    )
    assert out["route"] == "done" and ctx.route == "done"
    assert out.get("reason") == "confident_win_backstop"


def test_controller_rejection_does_not_reappend():
    # Compile already recorded the rejection (attempt bumped there); the
    # controller routes without touching history a second time.
    ctx = FakeCtx(
        _inv_state(max_attempts=5,
                   validation_attempt_count=1,
                   experiment_history=[],
                   rejected_history=["bad", "k: unknown"])
    )
    rej = CompileRejection(reason="bad", errors=["k: unknown"], design_name="e9")
    out = nodes.confirmation_controller(ctx, rej)
    assert out["route"] == "retry"
    assert out["validation_attempt_count"] == 1 and ctx.state["validation_attempt_count"] == 1
    assert ctx.state["experiment_history"] == []
    assert ctx.state["rejected_history"] == ["bad", "k: unknown"]


def test_screen_records_row_and_bumps_attempt_once():
    ctx = FakeCtx(
        {
            "shared_baseline": {"per_run_tps": [100.0, 101.0, 102.0]},
            "baseline_cache_key": nodes._baseline_cache_key(None, None),
            "min_improvement_pct": 2.0,
            "max_attempts": 6,
            "success_candidates": 1,
        }
    )
    out = nodes.screen_candidate(ctx, _compiled(), validate_fn=_mock_validate_ok)
    assert isinstance(out, ScreenVerdict)
    assert ctx.state["validation_attempt_count"] == 1
    assert len(ctx.state["experiment_history"]) == 1
    row = ctx.state["experiment_history"][0]
    assert row["name"] == "e1" and row["status"] == "PASS"
    # A controller visit afterwards must not recount.
    ctrl = nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="retry_same", targets=[], rationale="flake?", confidence=0.6),
    )
    assert ctx.state["validation_attempt_count"] == 1
    assert len(ctx.state["experiment_history"]) == 1
    assert ctrl["route"] == "done"  # quota met (1/1): confident win backstop
    assert ctrl.get("reason") == "confident_win_backstop"


def test_compile_rejection_records_and_bumps_attempt_once():
    ctx = FakeCtx(_inv_state(max_attempts=5))
    out = nodes.compile_candidate(
        ctx, _proposal(levels=[{"knob": "nope", "value": "1"}])
    )
    assert isinstance(out, CompileRejection)
    assert ctx.state["validation_attempt_count"] == 1
    assert any("not in available knob inventory" in r for r in ctx.state["rejected_history"])
    # Controller routes the recorded rejection without recounting.
    ctrl = nodes.confirmation_controller(ctx, out)
    assert ctrl["route"] == "retry"
    assert ctx.state["validation_attempt_count"] == 1


# --- screen: verdict mapping + never None ---


def _mock_validate_ok(**kwargs):
    return {
        "status": "PASS",
        "paired": {
            "baseline": {"per_run_tps": [100.0, 101.0, 102.0]},
            "tuned": {"per_run_tps": [110.0, 112.0, 111.0]},
        },
        "reasons": [],
        "stopped_early": False,
    }


def _compiled():
    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    return CompiledPlan(plan=plan.model_dump(), exp_name="e1", phase="screen",
                        valid_knobs=["work_mem"])


def test_screen_pass_maps_row():
    ctx = FakeCtx(
        {
            "shared_baseline": {"per_run_tps": [100.0, 101.0, 102.0]},
            "baseline_cache_key": nodes._baseline_cache_key(None, None),
            "min_improvement_pct": 2.0,
        }
    )
    out = nodes.screen_candidate(ctx, _compiled(), validate_fn=_mock_validate_ok)
    assert isinstance(out, ScreenVerdict)
    assert out.status == "PASS" and out.confirmed is True
    assert out.mean_delta_pct > 0
    assert out.paired  # paired evidence preserved


def test_screen_baseline_runs_once_and_caches():
    calls = []

    def _validate(**kwargs):
        calls.append(kwargs)
        if kwargs.get("baseline_only"):
            return {"baseline": {"per_run_tps": [100.0, 101.0]}, "status": "ok"}
        return _mock_validate_ok(**kwargs)

    ctx = FakeCtx({"min_improvement_pct": 2.0})
    first = nodes.screen_candidate(ctx, _compiled(), validate_fn=_validate)
    assert isinstance(first, ScreenVerdict)
    assert ctx.state.get("shared_baseline") == {"per_run_tps": [100.0, 101.0]}
    n_calls = len(calls)
    second = nodes.screen_candidate(ctx, _compiled(), validate_fn=_validate)
    assert isinstance(second, ScreenVerdict)
    assert len(calls) == n_calls + 1  # only the arm run, no second baseline


def test_screen_fail_value_on_error_never_none():
    def _boom(**kwargs):
        raise RuntimeError("no db")

    ctx = FakeCtx({"shared_baseline": {"per_run_tps": [1.0]}})
    out = nodes.screen_candidate(ctx, _compiled(), validate_fn=_boom)
    assert isinstance(out, ScreenVerdict)
    assert out.status != "PASS" or out.confirmed is False
    assert out is not None

    out2 = nodes.screen_candidate(FakeCtx({}), _compiled(), validate_fn=None)
    assert isinstance(out2, ScreenVerdict)


# --- decision ---


def _row(mean=5.0, lcb=3.0, status="PASS", confirmed=True, ucb=7.0, arm="e1"):
    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    return {
        "arm": arm,
        "phase": "screen",
        "status": status,
        "mean_delta_pct": mean,
        "lcb_pct": lcb,
        "ucb_pct": ucb,
        "df": 4.0,
        "confirmed": confirmed,
        "improvement_confident": lcb > 2.0,
        "paired": {"baseline": {}, "tuned": {}},
        "reasons": [],
        "plan": plan,
    }


def test_decision_strong_win():
    ctx = FakeCtx({"min_improvement_pct": 2.0})
    out = nodes.decision(ctx, all_rows=[_row()], baseline_tps=[100.0])
    assert isinstance(out, TerminalDecision)
    assert out.decision == "apply_winner"
    assert out.winner_plan["knobs"][0]["name"] == "work_mem"
    assert out.summary["archive"]["experiments_run"] == 1


def test_decision_inconclusive_vs_fail():
    ctx = FakeCtx({"min_improvement_pct": 2.0})
    bad = _row(status="FAIL", confirmed=False)
    out = nodes.decision(ctx, all_rows=[bad], baseline_tps=[100.0])
    assert out.decision == "inconclusive"
    bad_nopair = dict(bad, paired=None)
    out2 = nodes.decision(FakeCtx({}), all_rows=[bad_nopair], baseline_tps=[])
    assert out2.decision == "fail"


def test_decision_confirmed_pass_below_threshold_withholds():
    # Compounding quota unification: "winner" means lcb > min_improvement_pct,
    # so a confirmed PASS whose LCB misses the bar no longer applies.
    ctx = FakeCtx({"min_improvement_pct": 5.0})
    row = _row(mean=1.0, lcb=0.5)
    out = nodes.decision(ctx, all_rows=[row], baseline_tps=[100.0])
    assert out.decision != "apply_winner"
    assert out.winner_plan == {}


def test_decision_picks_best_confirmed_pass():
    ctx = FakeCtx({"min_improvement_pct": 5.0})
    rows = [_row(mean=8.0, lcb=6.0), _row(mean=12.0, lcb=9.0)]
    out = nodes.decision(ctx, all_rows=rows, baseline_tps=[100.0])
    assert out.decision == "apply_winner"
    assert out.summary["stats"]["mean_delta_pct"] == 12.0


def test_decision_unconfirmed_pass_withholds():
    ctx = FakeCtx({"min_improvement_pct": 2.0})
    row = _row(confirmed=False)
    out = nodes.decision(ctx, all_rows=[row], baseline_tps=[100.0])
    assert out.decision != "apply_winner"
    assert out.winner_plan == {}


def test_decision_lead_decisive():
    ctx = FakeCtx({"min_improvement_pct": 2.0})
    winner = _row(mean=5.0, lcb=4.0, ucb=6.0, arm="e1")
    runner = _row(mean=3.0, lcb=2.0, ucb=3.0, arm="e2")
    out = nodes.decision(ctx, all_rows=[winner, runner], baseline_tps=[100.0])
    assert out.decision == "apply_winner"
    assert out.summary["stats"]["mean_delta_pct"] == 5.0
    archive = out.summary["archive"]
    assert archive["lead"] == "decisive"
    assert any("decisive" in r for r in out.summary["reasons"])


def test_decision_lead_overlapping():
    ctx = FakeCtx({"min_improvement_pct": 2.0})
    winner = _row(mean=5.0, lcb=3.0, ucb=7.0, arm="e1")
    runner = _row(mean=3.0, lcb=2.0, ucb=6.0, arm="e2")
    out = nodes.decision(ctx, all_rows=[winner, runner], baseline_tps=[100.0])
    assert out.decision == "apply_winner"
    # Winner stays the same max(mean, lcb) arm.
    assert out.summary["stats"]["mean_delta_pct"] == 5.0
    archive = out.summary["archive"]
    assert archive["lead"] == "overlapping"
    assert archive["runner_up"] == "e2"
    assert any("overlapping" in r and "e2" in r for r in out.summary["reasons"])


# --- preflight routing ---


def _terminal(knobs):
    return TerminalDecision(
        decision="apply_winner",
        winner_plan={"knobs": knobs},
        summary={},
    )


def test_preflight_auto_live():
    ctx = FakeCtx({"apply_mode": "live", "durability_profile": "strict"})
    out = nodes.production_preflight(
        ctx, _terminal([{"name": "work_mem", "value": "64MB", "scope": "user"}])
    )
    assert isinstance(out, PreflightVerdict)
    assert out.route == "auto"


def test_preflight_auto_on_restart_knob_under_live():
    ctx = FakeCtx({"apply_mode": "live"})
    out = nodes.production_preflight(
        ctx,
        _terminal(
            [{"name": "shared_buffers", "value": "1GB", "scope": "postmaster",
              "restart_required": True}]
        ),
    )
    assert out.route == "auto"
    assert "persisted under live" in out.reason


def test_preflight_maintenance_on_restart_knob_under_manual():
    ctx = FakeCtx({"apply_mode": "manual"})
    out = nodes.production_preflight(
        ctx,
        _terminal(
            [{"name": "shared_buffers", "value": "1GB", "scope": "postmaster",
              "restart_required": True}]
        ),
    )
    assert out.route == "maintenance_assisted"


def test_preflight_blocked_on_empty_plan():
    ctx = FakeCtx({})
    out = nodes.production_preflight(ctx, _terminal([]))
    assert out.route == "blocked"


# --- materialize + prepare ---


def test_materialize_inventory_writes_file_and_state(tmp_path):
    ctx = FakeCtx({"knob_path": str(tmp_path)})
    output = DbInspectorOutput(
        db_type="postgres",
        db_version="16.1",
        tables=[{"name": "t", "approximate_row_count": 10}],
        available_knobs=[
            {"name": "work_mem", "current_value": "4MB", "context": "user"},
            {"name": "pg_internals", "current_value": "x", "context": "internal"},
        ],
    )
    out = nodes.materialize_inventory(ctx, output)
    assert out["n_knobs"] == 2 and out["db_version"] == "16.1"
    assert ctx.state["available_knob_names"] == ["work_mem"]
    assert ctx.state["schema_info"][0]["name"] == "t"
    assert ctx.state["db_version"] == "16.1"
    with open(os.path.join(str(tmp_path), "knobs.json"), encoding="utf-8") as f:
        assert len(json.load(f)) == 2


def test_prepare_run_resolves_and_inits(tmp_path):
    ctx = FakeCtx(
        {
            "resource_budget": {"cpu_cores": 4, "memory_gb": 8.0},
            "max_attempts": 6,
            "run_id": "r1",
            "run_dir": str(tmp_path),
        }
    )
    out = nodes.prepare_run(ctx)
    assert out["resource_budget"] == {"cpu_cores": 4, "memory_gb": 8.0}
    assert out["max_attempts"] == 6
    assert out["measure_reps"] >= 2
    assert out["measure_seconds"] >= 1
    assert out["measure_warmup_seconds"] >= 0
    assert ctx.state["validation_attempt_count"] == 0
    assert ctx.state["experiment_history"] == []
    assert ctx.state["run_config"]["run_id"] == "r1"


# --- confirmation run (Part 4) ---


def _confirm_row(value, mean=12.0, lcb=9.0, status="PASS", confirmed=True):
    from src.knob_tuner.contracts import KnobPlan

    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": value, "scope": "user"}]}
    )
    return {
        "arm": "e",
        "phase": "screen",
        "status": status,
        "mean_delta_pct": mean,
        "lcb_pct": lcb,
        "ucb_pct": mean + 1.0,
        "confirmed": confirmed,
        "improvement_confident": lcb > 2.0,
        "paired": {"baseline": {}, "tuned": {}},
        "reasons": [],
        "plan": plan,
    }


def _confirm_ctx(rows, **over):
    state = {
        "min_improvement_pct": 5.0,
        "max_attempts": 20,
        "success_candidates": 3,
        "shared_baseline": {"per_run_tps": [100.0, 101.0, 102.0]},
        "baseline_cache_key": nodes._baseline_cache_key(None, None),
        "experiment_history": [],
        "all_rows": rows,
    }
    state.update(over)
    return FakeCtx(state)


def _validate_by_value(pass_values):
    """Return a validate_fn that PASSes only plans whose work_mem value is in
    pass_values; otherwise FAILs. Baseline-only calls return a baseline."""

    def _v(plan=None, run_profile=None, shared_baseline=None, attempt=0,
           early_stop_min_reps=None, baseline_only=False, **kwargs):
        if baseline_only:
            return {"baseline": {"per_run_tps": [100.0, 101.0, 102.0]}}
        v = None
        for spec in getattr(plan, "knobs", []) or []:
            if getattr(spec, "name", "") == "work_mem":
                v = spec.value
        if v in pass_values:
            return {"status": "PASS",
                    "paired": {"baseline": {"per_run_tps": [100.0, 101.0, 102.0]},
                               "tuned": {"per_run_tps": [112.0, 113.0, 114.0]}},
                    "reasons": [], "stopped_early": False}
        return {"status": "FAIL",
                "paired": {"baseline": {"per_run_tps": [100.0, 101.0, 102.0]},
                           "tuned": {"per_run_tps": [99.0, 98.0, 97.0]}},
                "reasons": ["no improvement"], "stopped_early": False}

    return _v


def _term(rows):
    winner = _plan_dump(rows[0]["plan"])
    return TerminalDecision(
        decision="apply_winner",
        winner_plan=winner,
        summary={"status": "PASS", "plan_hash": "", "reasons": []},
    )


def test_confirm_winner_keeps_winner_when_fresh_verdict_passes():
    rows = [_confirm_row("64MB", mean=12.0), _confirm_row("32MB", mean=9.0)]
    ctx = _confirm_ctx(rows)
    out = nodes.confirm_winner(
        ctx, _term(rows), validate_fn=_validate_by_value({"64MB"})
    )
    assert out.decision == "apply_winner"
    assert out.summary["confirmation"]["verdict"] == "pass"
    assert out.summary["confirmation"]["promoted"] is False
    assert out.summary["confirmation"]["attempted"] == 1


def test_confirm_winner_promotes_runner_up_when_winner_fails():
    rows = [_confirm_row("64MB", mean=12.0), _confirm_row("32MB", mean=9.0)]
    ctx = _confirm_ctx(rows)
    out = nodes.confirm_winner(
        ctx, _term(rows), validate_fn=_validate_by_value({"32MB"})
    )
    assert out.decision == "apply_winner"
    assert out.summary["confirmation"]["verdict"] == "pass"
    assert out.summary["confirmation"]["promoted"] is True
    assert out.summary["confirmation"]["attempted"] == 2
    assert out.winner_plan["knobs"][0]["value"] == "32MB"


def test_confirm_winner_withholds_when_both_fail():
    rows = [_confirm_row("64MB", mean=12.0), _confirm_row("32MB", mean=9.0)]
    ctx = _confirm_ctx(rows)
    out = nodes.confirm_winner(
        ctx, _term(rows), validate_fn=_validate_by_value(set())
    )
    assert out.decision == "inconclusive"
    assert out.winner_plan == {}
    assert out.summary["confirmation"]["verdict"] == "fail"
    assert out.summary["confirmation"]["attempted"] == 2
    assert any("confirmation" in r for r in out.summary["reasons"])


def test_confirm_winner_skips_non_winner_decision():
    ctx = _confirm_ctx([])
    term = TerminalDecision(decision="inconclusive", winner_plan={},
                            summary={"reasons": []})
    out = nodes.confirm_winner(ctx, term, validate_fn=_validate_by_value({"64MB"}))
    assert out.decision == "inconclusive"
    assert out.summary["confirmation"]["verdict"] == "skipped"


def _plan_dump(plan):
    from src.knob_tuner.contracts import KnobPlan

    if isinstance(plan, KnobPlan):
        return plan.model_dump()
    return dict(plan)


def test_confirm_winner_walks_pool_to_a_lower_candidate_without_cap():
    # No fixed confirmation cap: the 1st and 2nd candidates fail, the 3rd
    # passes → the 3rd is applied (proves the walk continues past 2).
    rows = [
        _confirm_row("64MB", mean=12.0),
        _confirm_row("32MB", mean=9.0),
        _confirm_row("16MB", mean=7.0),
    ]
    ctx = _confirm_ctx(rows)
    out = nodes.confirm_winner(
        ctx, _term(rows), validate_fn=_validate_by_value({"16MB"})
    )
    assert out.decision == "apply_winner"
    assert out.summary["confirmation"]["attempted"] == 3
    assert out.summary["confirmation"]["promoted"] is True
    assert out.winner_plan["knobs"][0]["value"] == "16MB"


def test_confirm_winner_no_cap_walks_whole_pool_when_none_pass():
    rows = [
        _confirm_row("64MB", mean=12.0),
        _confirm_row("32MB", mean=9.0),
        _confirm_row("16MB", mean=7.0),
        _confirm_row("8MB", mean=6.0),
    ]
    ctx = _confirm_ctx(rows)
    out = nodes.confirm_winner(
        ctx, _term(rows), validate_fn=_validate_by_value(set())
    )
    assert out.decision == "inconclusive"
    assert out.summary["confirmation"]["attempted"] == 4
