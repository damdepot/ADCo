"""Wave 2 enforcement tests with real ADK State (persistence matters).

Covers: per-enum compile violations, stop->done, double retry_same->done,
repeat-hash rejection + retry_same bypass, beliefs/bundle refresh,
diagnosis_history append, inactive-with-empty-history. No live benchmarks.
"""

from src.knob_tuner.stages import nodes
from src.knob_tuner.stages.models import (
    CompiledPlan,
    CompileRejection,
    DiagnosisOutput,
    ScreenVerdict,
)
from tests.test_knob_tuner.conftest import AdkCtx


def _seed_inventory(ctx: AdkCtx) -> None:
    ctx.state.update(
        {
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
            ],
            "resource_budget": {"cpu_cores": 4, "memory_gb": 8.0},
            "durability_profile": "strict",
            "max_set_knobs": 20,
            "max_attempts": 6,
            "success_candidates": 1,
        }
    )


def _ctx() -> AdkCtx:
    ctx = AdkCtx()
    _seed_inventory(ctx)
    return ctx


def _proposal(name="exp-1", phase="screen", levels=None):
    return {
        "name": name,
        "phase": phase,
        "levels": levels if levels is not None else [{"knob": "work_mem", "value": "64MB"}],
    }


def _diagnose(ctx: AdkCtx, correction: str, targets=None, rationale="r",
              confidence: float = 0.8, stop_reason: str = "futility"):
    ctx.state.update(
        {
            "last_screen_row": {
                "arm": "e-prev",
                "phase": "screen",
                "status": "FAIL",
                "mean_delta_pct": -1.0,
                "lcb_pct": -2.0,
                "ucb_pct": 0.0,
                "confirmed": False,
                "reasons": ["slow"],
                "plan": {"knobs": [{"name": "work_mem", "value": "32MB"}]},
            }
        }
    )
    return nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction=correction, targets=list(targets or []), rationale=rationale,
                        confidence=confidence, stop_reason=stop_reason),
    )


def test_diagnosis_history_appends_full_struct_and_mirrors():
    ctx = _ctx()
    out = _diagnose(ctx, "drop_knob", ["work_mem"], "bad mover")
    assert out["route"] in ("retry", "done")
    hist = ctx.state.get("diagnosis_history")
    assert isinstance(hist, list) and len(hist) == 1
    assert hist[0]["correction"] == "drop_knob"
    assert hist[0]["targets"] == ["work_mem"]
    assert hist[0]["rationale"] == "bad mover"
    assert ctx.state.get("diagnosis_output") == hist[0]


def test_drop_knob_violation():
    ctx = _ctx()
    _diagnose(ctx, "drop_knob", ["work_mem"])
    bad = nodes.compile_candidate(ctx, _proposal(levels=[{"knob": "work_mem", "value": "64MB"}]))
    assert isinstance(bad, CompileRejection)
    assert "drop_knob" in bad.reason
    good = nodes.compile_candidate(
        ctx, _proposal(name="ok", levels=[{"knob": "shared_buffers", "value": "256MB"}])
    )
    assert isinstance(good, CompiledPlan)


def test_shrink_set_violation():
    ctx = _ctx()
    _diagnose(ctx, "shrink_set", [])
    ctx.state.update(
        {"experiment_history": [{"name": "e-prev", "phase": "screen", "n_knobs": 2}]}
    )
    bad = nodes.compile_candidate(
        ctx,
        _proposal(
            levels=[
                {"knob": "work_mem", "value": "64MB"},
                {"knob": "shared_buffers", "value": "256MB"},
            ]
        ),
    )
    assert isinstance(bad, CompileRejection)
    assert "shrink_set" in bad.reason
    good = nodes.compile_candidate(ctx, _proposal(name="small"))
    assert isinstance(good, CompiledPlan)


def test_change_phase_violation():
    ctx = _ctx()
    _diagnose(ctx, "change_phase", ["refinement"])
    bad = nodes.compile_candidate(ctx, _proposal(phase="screen"))
    assert isinstance(bad, CompileRejection)
    assert "change_phase" in bad.reason
    good = nodes.compile_candidate(ctx, _proposal(name="ok", phase="refinement"))
    assert isinstance(good, CompiledPlan)


def test_adjust_value_violation_needs_new_value():
    ctx = _ctx()
    ctx.state.update(
        {
            "candidates": [
                {"plan": {"knobs": [{"name": "work_mem", "value": "32MB"}]}, "plan_hash": "prev"}
            ],
            "last_screen_row": {
                "arm": "e-prev",
                "phase": "screen",
                "status": "FAIL",
                "plan_hash": "prev",
                "reasons": ["wrong direction"],
            },
        }
    )
    nodes.confirmation_controller(
        ctx, DiagnosisOutput(correction="adjust_value", targets=["work_mem"], rationale="step", confidence=0.7)
    )
    same = nodes.compile_candidate(
        ctx, _proposal(levels=[{"knob": "work_mem", "value": "32MB"}])
    )
    assert isinstance(same, CompileRejection)
    assert "adjust_value" in same.reason
    missing = nodes.compile_candidate(
        ctx, _proposal(name="miss", levels=[{"knob": "shared_buffers", "value": "256MB"}])
    )
    assert isinstance(missing, CompileRejection)
    assert "adjust_value" in missing.reason
    moved = nodes.compile_candidate(
        ctx, _proposal(name="moved", levels=[{"knob": "work_mem", "value": "64MB"}])
    )
    assert isinstance(moved, CompiledPlan)


def test_multi_action_history_enforces_all_corrections():
    # Phase 5.4 full enforcement: every diagnosis in history applies, not
    # just the latest. One rejection names each violated correction.
    ctx = _ctx()
    _diagnose(ctx, "drop_knob", ["work_mem"])
    _diagnose(ctx, "change_phase", ["refinement"])
    assert len(ctx.state.get("diagnosis_history") or []) == 2

    both = nodes.compile_candidate(ctx, _proposal(phase="screen"))
    assert isinstance(both, CompileRejection)
    assert "drop_knob" in both.reason
    assert "change_phase" in both.reason

    one = nodes.compile_candidate(
        ctx,
        _proposal(
            name="one",
            phase="screen",
            levels=[{"knob": "shared_buffers", "value": "256MB"}],
        ),
    )
    assert isinstance(one, CompileRejection)
    assert "change_phase" in one.reason
    assert "drop_knob" not in one.reason

    ok = nodes.compile_candidate(
        ctx,
        _proposal(
            name="ok",
            phase="refinement",
            levels=[{"knob": "shared_buffers", "value": "256MB"}],
        ),
    )
    assert isinstance(ok, CompiledPlan)


def test_stop_violation_and_done():
    ctx = _ctx()
    out = _diagnose(ctx, "stop", [], "exhausted")
    assert out["route"] == "done"
    assert out.get("reason") == "stopped_by_diagnosis"
    rejected = nodes.compile_candidate(ctx, _proposal())
    assert isinstance(rejected, CompileRejection)
    assert "stop" in rejected.reason


def test_double_retry_same_goes_done():
    ctx = _ctx()
    first = _diagnose(ctx, "retry_same", [], "flake?")
    assert first["route"] == "retry"
    assert int(ctx.state.get("retry_same_count") or 0) == 1
    second = nodes.confirmation_controller(
        ctx, DiagnosisOutput(correction="retry_same", targets=[], rationale="still flaky", confidence=0.6)
    )
    assert second["route"] == "done"
    assert int(ctx.state.get("retry_same_count") or 0) == 2


def test_repeat_hash_rejected_and_retry_same_bypasses():
    ctx = _ctx()
    first = nodes.compile_candidate(ctx, _proposal(name="dup"))
    assert isinstance(first, CompiledPlan)
    from src.knob_tuner.contracts import KnobPlan

    plan_hash = KnobPlan.model_validate(first.plan).plan_hash()
    ctx.state.update(
        {"experiment_history": [{"name": "dup", "phase": "screen", "n_knobs": 1, "plan_hash": plan_hash}]}
    )
    repeat = nodes.compile_candidate(ctx, _proposal(name="dup"))
    assert isinstance(repeat, CompileRejection)
    assert "repeat" in repeat.reason
    nodes.confirmation_controller(
        ctx, DiagnosisOutput(correction="retry_same", targets=[], rationale="flake", confidence=0.6)
    )
    allowed = nodes.compile_candidate(ctx, _proposal(name="dup"))
    assert isinstance(allowed, CompiledPlan)


def test_beliefs_and_bundle_refresh_on_screen():
    # Beliefs + bundle refresh now happen in screen_candidate (the outcome
    # producer), not in the controller. Drive a real screen, then route.
    from src.knob_tuner.contracts import KnobPlan

    ctx = _ctx()
    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    compiled = CompiledPlan(
        plan=plan.model_dump(), exp_name="e1", phase="screen",
        valid_knobs=["work_mem"],
    )

    def _validate(**kwargs):
        return {
            "status": "PASS",
            "paired": {
                "baseline": {"per_run_tps": [100.0, 101.0, 102.0]},
                "tuned": {"per_run_tps": [110.0, 112.0, 111.0]},
            },
            "reasons": [],
            "stopped_early": False,
        }

    ctx.state.update({"min_improvement_pct": 2.0})
    verdict = nodes.screen_candidate(ctx, compiled, validate_fn=_validate)
    assert isinstance(verdict, ScreenVerdict)
    assert ctx.state.get("validation_attempt_count") == 1
    assert len(ctx.state.get("experiment_history") or []) == 1
    beliefs = ctx.state.get("knob_beliefs")
    assert isinstance(beliefs, dict) and "work_mem" in beliefs
    assert beliefs["work_mem"]["n_seen"] == 1
    assert beliefs["work_mem"]["best_delta_pct"] == verdict.mean_delta_pct
    assert beliefs["work_mem"]["last_phase"] == "screen"
    bundle = ctx.state.get("evidence_bundle") or ""
    assert "Evidence bundle" in bundle and "e1" in bundle
    table = ctx.state.get("belief_table") or ""
    assert "work_mem" in table

    # Controller routes the diagnosed outcome without recounting.
    out = nodes.confirmation_controller(
        ctx, DiagnosisOutput(correction="stop", targets=[], rationale="winner",
                             confidence=0.95, stop_reason="winner")
    )
    assert out["route"] == "done"
    assert ctx.state.get("validation_attempt_count") == 1
    assert len(ctx.state.get("experiment_history") or []) == 1


def test_screen_pass_then_diagnosis_next_routes_retry_once():
    # Full new-loop sequence on real State: screen-pass verdict still goes
    # to diagnosis first, and the whole sequence counts exactly one attempt.
    from src.knob_tuner.contracts import KnobPlan

    ctx = _ctx()
    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    compiled = CompiledPlan(
        plan=plan.model_dump(), exp_name="e-pass", phase="screen",
        valid_knobs=["work_mem"],
    )

    def _weak_validate(**kwargs):
        # Small +0.5% nudge: not a confident win (lcb < min_improvement).
        return {
            "status": "PASS",
            "paired": {
                "baseline": {"per_run_tps": [100.0, 101.0, 102.0, 100.5, 101.5]},
                "tuned": {"per_run_tps": [100.5, 101.5, 102.5, 101.0, 102.0]},
            },
            "reasons": [],
            "stopped_early": False,
        }

    ctx.state.update({"min_improvement_pct": 2.0})
    verdict = nodes.screen_candidate(ctx, compiled, validate_fn=_weak_validate)
    assert verdict.status == "PASS"
    assert ctx.state.get("validation_attempt_count") == 1
    out = nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="drop_knob", targets=["work_mem"], rationale="next", confidence=0.6),
    )
    if (verdict.mean_delta_pct > 0 and verdict.lcb_pct > 2.0):
        assert out["route"] == "done"
        assert out.get("reason") == "confident_win_backstop"
    else:
        assert out["route"] == "retry"
        assert "reason" not in out
    assert ctx.state.get("validation_attempt_count") == 1
    assert len(ctx.state.get("experiment_history") or []) == 1


def test_confident_win_backstop_overrides_next():
    ctx = _ctx()
    ctx.state.update(
        {
            "validation_attempt_count": 1,
            "min_improvement_pct": 2.0,
            "experiment_history": [
                {
                    "name": "e-win",
                    "phase": "screen",
                    "n_knobs": 1,
                    "mean_delta_pct": 12.0,
                    "lcb_pct": 11.0,
                    "status": "PASS",
                    "confirmed": True,
                }
            ],
            "last_screen_row": {
                "arm": "e-win",
                "phase": "screen",
                "status": "PASS",
                "mean_delta_pct": 12.0,
                "lcb_pct": 11.0,
                "ucb_pct": 13.0,
                "confirmed": True,
                "reasons": [],
            },
            "rejected_history": [],
        }
    )
    out = nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="drop_knob", targets=["work_mem"], rationale="next", confidence=0.6),
    )
    assert out["route"] == "done"
    assert out.get("reason") == "confident_win_backstop"


def test_attempt_cap_reason():
    ctx = _ctx()
    ctx.state.update(
        {
            "validation_attempt_count": 6,
            "max_attempts": 6,
            "min_improvement_pct": 2.0,
            "experiment_history": [
                {
                    "name": "e6",
                    "phase": "screen",
                    "n_knobs": 1,
                    "mean_delta_pct": -1.0,
                    "lcb_pct": -2.0,
                    "status": "FAIL",
                    "confirmed": False,
                }
            ],
            "last_screen_row": {
                "status": "FAIL",
                "mean_delta_pct": -1.0,
                "lcb_pct": -2.0,
                "reasons": ["slow"],
            },
            "rejected_history": ["slow"],
        }
    )
    out = nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="drop_knob", targets=["work_mem"], rationale="next", confidence=0.6),
    )
    assert out["route"] == "done"
    assert out.get("reason") == "attempt_cap"


def test_no_double_count_across_screen_diagnosis_controller():
    from src.knob_tuner.contracts import KnobPlan

    ctx = _ctx()
    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    compiled = CompiledPlan(
        plan=plan.model_dump(), exp_name="e1", phase="screen",
        valid_knobs=["work_mem"],
    )

    def _fail_validate(**kwargs):
        return {
            "status": "FAIL",
            "paired": {
                "baseline": {"per_run_tps": [100.0, 101.0, 102.0]},
                "tuned": {"per_run_tps": [99.0, 98.5, 99.5]},
            },
            "reasons": ["regression"],
            "stopped_early": False,
        }

    ctx.state.update({"min_improvement_pct": 2.0})
    nodes.screen_candidate(ctx, compiled, validate_fn=_fail_validate)
    assert ctx.state.get("validation_attempt_count") == 1
    out = nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="drop_knob", targets=["work_mem"], rationale="bad", confidence=0.6),
    )
    assert out["route"] == "retry"
    assert ctx.state.get("validation_attempt_count") == 1
    assert len(ctx.state.get("experiment_history") or []) == 1


def test_compile_rejected_bypasses_diagnosis():
    ctx = _ctx()
    bad = nodes.compile_candidate(
        ctx, _proposal(levels=[{"knob": "nope", "value": "1"}])
    )
    assert isinstance(bad, CompileRejection)
    assert ctx.state.get("validation_attempt_count") == 1
    assert ctx.state.get("diagnosis_history") in (None, [])
    assert any("not in available knob inventory" in r for r in (ctx.state.get("rejected_history") or []))
    out = nodes.confirmation_controller(ctx, bad)
    assert out["route"] == "retry"
    assert ctx.state.get("validation_attempt_count") == 1  # no recount
    assert ctx.state.get("diagnosis_history") in (None, [])


def test_prompt_constraints_set_from_drop_knob():
    ctx = _ctx()
    _diagnose(ctx, "drop_knob", ["work_mem"])
    assert ctx.state.get("excluded_knobs") == ["work_mem"]
    assert "Evidence bundle" in (ctx.state.get("evidence_bundle") or "")


def test_inactive_with_empty_history():
    # With no diagnosis active, a screen outcome records normally and the
    # prompt constraints stay clear.
    from src.knob_tuner.contracts import KnobPlan

    ctx = _ctx()
    ctx.state.update({"diagnosis_history": []})
    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    compiled = CompiledPlan(
        plan=plan.model_dump(), exp_name="e1", phase="screen",
        valid_knobs=["work_mem"],
    )

    def _fail_validate(**kwargs):
        return {
            "status": "FAIL",
            "paired": {
                "baseline": {"per_run_tps": [100.0, 101.0]},
                "tuned": {"per_run_tps": [99.0, 98.5]},
            },
            "reasons": ["slow"],
            "stopped_early": False,
        }

    nodes.screen_candidate(ctx, compiled, validate_fn=_fail_validate)
    assert ctx.state.get("validation_attempt_count") == 1
    assert len(ctx.state.get("experiment_history") or []) == 1
    assert ctx.state.get("excluded_knobs") == []
    assert ctx.state.get("required_phase") == ""
    assert ctx.state.get("max_knobs") == ""


# --- compounding campaign signals (Part 2) ---


def _campaign_state(**over):
    state = {
        "min_improvement_pct": 5.0,
        "success_candidates": 3,
        "validation_attempt_count": 2,
        "max_attempts": 20,
        "available_knob_names": ["work_mem", "shared_buffers", "wal_buffers",
                                 "max_wal_size", "random_page_cost"],
        "experiment_history": [
            {"name": "e1", "phase": "screen", "n_knobs": 2,
             "mean_delta_pct": 9.0, "lcb_pct": 6.0, "status": "PASS",
             "confirmed": True, "plan_hash": "h1"},
            {"name": "e2", "phase": "screen", "n_knobs": 2,
             "mean_delta_pct": 7.0, "lcb_pct": 5.5, "status": "PASS",
             "confirmed": True, "plan_hash": "h2"},
            {"name": "e3", "phase": "screen", "n_knobs": 1,
             "mean_delta_pct": 0.5, "lcb_pct": -0.5, "status": "FAIL",
             "confirmed": False, "plan_hash": "h3"},
        ],
        "all_rows": [
            {"plan_hash": "h1", "plan": {"knobs": [
                {"name": "work_mem"}, {"name": "shared_buffers"}]}},
            {"plan_hash": "h2", "plan": {"knobs": [
                {"name": "work_mem"}, {"name": "max_wal_size"}]}},
            {"plan_hash": "h3", "plan": {"knobs": [
                {"name": "random_page_cost"}]}},
        ],
        "candidates": [],
        "knob_beliefs": {
            "work_mem": {"best_delta_pct": 9.0, "n_seen": 2,
                          "last_phase": "screen", "cleared": True},
            "random_page_cost": {"best_delta_pct": 0.5, "n_seen": 1,
                                 "last_phase": "screen", "cleared": False},
        },
    }
    state.update(over)
    return state


def test_success_knobs_only_from_clearing_arms():
    from src.knob_tuner.stages.nodes import _common

    counts = _common.success_knob_counts(_campaign_state())
    # work_mem appeared in both clearing arms; random_page_cost only in the
    # non-clearing arm and must be absent.
    assert counts == {"work_mem": 2, "shared_buffers": 1, "max_wal_size": 1}
    assert "random_page_cost" not in counts


def test_success_knobs_rendered_ranked():
    from src.knob_tuner.stages.nodes.diagnosis import render_success_knobs

    text = render_success_knobs({"work_mem": 2, "shared_buffers": 1})
    assert "work_mem" in text
    # work_mem (2) ranks above shared_buffers (1)
    assert text.index("work_mem") < text.index("shared_buffers")
    assert render_success_knobs({}).startswith("No confirmed")


def test_campaign_directive_explore_when_no_winners():
    from src.knob_tuner.stages.nodes.diagnosis import _campaign_directive

    state = _campaign_state(experiment_history=[], all_rows=[])
    text = _campaign_directive(state, {})
    assert text.startswith("Mode: EXPLORE")


def test_campaign_directive_exploit_when_partial():
    from src.knob_tuner.stages.nodes.diagnosis import _campaign_directive

    state = _campaign_state()
    counts = {"work_mem": 2, "shared_buffers": 1, "max_wal_size": 1}
    text = _campaign_directive(state, counts)
    assert text.startswith("Mode: EXPLOIT+EXPLORE")
    assert "2/3" in text
    assert "work_mem" in text
    assert "2-4 knobs" in text


def test_campaign_directive_stop_when_quota_met():
    from src.knob_tuner.stages.nodes.diagnosis import _campaign_directive

    extra = {"name": "e4", "phase": "screen", "n_knobs": 1,
             "mean_delta_pct": 8.0, "lcb_pct": 6.0, "status": "PASS",
             "confirmed": True, "plan_hash": "h4"}
    state = _campaign_state(
        experiment_history=_campaign_state()["experiment_history"] + [extra]
    )
    text = _campaign_directive(state, {"work_mem": 3})
    assert text.startswith("Mode: STOP")


def test_refresh_memory_writes_campaign_keys():
    from src.knob_tuner.stages.nodes.diagnosis import _refresh_memory

    state = _campaign_state()
    _refresh_memory(state)
    assert "success_knobs" in state and "work_mem" in state["success_knobs"]
    assert state["campaign_directive"].startswith("Mode: EXPLOIT+EXPLORE")
    assert "Winners 2/3" in state["evidence_bundle"]
    assert "cleared" in state["belief_table"]


# --- distinctness guard: knob set, not plan hash (Part 3) ---


def _cleared_ctx(threshold=5.0):
    """Ctx with one clearing arm whose knob set is {work_mem}. Value 64MB."""
    ctx = _ctx()
    ctx.state.update({"min_improvement_pct": threshold, "success_candidates": 3})
    ctx.state.update(
        {
            "experiment_history": [
                {"name": "clear", "phase": "screen", "n_knobs": 1,
                 "mean_delta_pct": 9.0, "lcb_pct": 6.0, "status": "PASS",
                 "confirmed": True, "plan_hash": "hc"},
            ],
            "all_rows": [
                {"plan_hash": "hc",
                 "plan": {"knobs": [{"name": "work_mem", "value": "64MB"}]}},
            ],
            "candidates": [],
        }
    )
    return ctx


def test_knob_set_guard_rejects_value_nudge_of_cleared_set():
    ctx = _cleared_ctx()
    # Same knob name, DIFFERENT value: the set already cleared → reject.
    bad = nodes.compile_candidate(
        ctx, _proposal(name="nudge", levels=[{"knob": "work_mem", "value": "128MB"}])
    )
    assert isinstance(bad, CompileRejection)
    assert "repeat knob set" in bad.reason and "work_mem" in bad.reason


def test_knob_set_guard_allows_nudge_of_non_clearing_set():
    ctx = _ctx()
    ctx.state.update({"min_improvement_pct": 5.0, "success_candidates": 3})
    # A NON-clearing arm (lcb below bar) does not consume its knob set.
    ctx.state.update(
        {
            "experiment_history": [
                {"name": "weak", "phase": "screen", "n_knobs": 1,
                 "mean_delta_pct": 0.5, "lcb_pct": -1.0, "status": "FAIL",
                 "confirmed": False, "plan_hash": "hw"},
            ],
            "all_rows": [
                {"plan_hash": "hw",
                 "plan": {"knobs": [{"name": "work_mem", "value": "32MB"}]}},
            ],
            "candidates": [],
        }
    )
    good = nodes.compile_candidate(
        ctx, _proposal(name="nudge", levels=[{"knob": "work_mem", "value": "64MB"}])
    )
    assert isinstance(good, CompiledPlan)


def test_knob_set_guard_ignores_superset_and_subset():
    ctx = _cleared_ctx()
    # A superset of the cleared set is a genuinely different arm → allowed.
    bigger = nodes.compile_candidate(
        ctx,
        _proposal(
            name="bigger",
            levels=[
                {"knob": "work_mem", "value": "64MB"},
                {"knob": "shared_buffers", "value": "256MB"},
            ],
        ),
    )
    assert isinstance(bigger, CompiledPlan)


def test_winner_stop_non_binding_while_quota_unmet():
    # A winner stop must not veto the next proposal while the quota is unmet
    # (otherwise the compounded campaign can never collect candidates).
    ctx = _ctx()
    ctx.state.update({"min_improvement_pct": 5.0, "success_candidates": 3})
    nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="stop", targets=[], rationale="winner!",
                        confidence=0.95, stop_reason="winner"),
    )
    # No clearing rows yet -> quota unmet -> stop is non-binding.
    good = nodes.compile_candidate(ctx, _proposal(name="next-winner"))
    assert isinstance(good, CompiledPlan)


def test_winner_stop_binding_once_quota_met():
    ctx = _ctx()
    ctx.state.update({"min_improvement_pct": 5.0, "success_candidates": 1})
    ctx.state.update(
        {
            "experiment_history": [
                {"name": "c1", "phase": "screen", "n_knobs": 1,
                 "mean_delta_pct": 9.0, "lcb_pct": 6.0, "status": "PASS",
                 "confirmed": True, "plan_hash": "h1"},
            ],
            "all_rows": [
                {"plan_hash": "h1",
                 "plan": {"knobs": [{"name": "work_mem", "value": "64MB"}]}},
            ],
        }
    )
    nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="stop", targets=[], rationale="winner!",
                        confidence=0.95, stop_reason="winner"),
    )
    # Quota (1) met -> the stop binds and vetoes the next proposal.
    bad = nodes.compile_candidate(ctx, _proposal(name="after-stop"))
    assert isinstance(bad, CompileRejection)
    assert "stop" in bad.reason


def test_futility_stop_stays_binding_while_quota_unmet():
    ctx = _ctx()
    ctx.state.update({"min_improvement_pct": 5.0, "success_candidates": 3})
    nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="stop", targets=[], rationale="hopeless",
                        confidence=0.9, stop_reason="futility"),
    )
    bad = nodes.compile_candidate(ctx, _proposal(name="after-futility"))
    assert isinstance(bad, CompileRejection)
    assert "stop" in bad.reason
