"""Regression tests for the runaway-loop bug: nodes must use the LIVE ADK State.
Real ``google.adk.sessions.state.State`` is NOT a dict (no keys()/__iter__),
so the old ``_state()`` (``dict(ctx.state)``) raised internally and fell back
to a disconnected ``{}`` — reads saw empty state, writes were lost. Symptoms:
compile rejected every proposal (empty knobs_info) and confirmation_controller
recomputed attempt=1 forever (cap unreachable).

These tests drive confirmation_controller + compile_candidate +
materialize_inventory with a REAL ADK State object.
"""

import pytest
from google.adk.sessions.state import State

from src.knob_tuner.stages import nodes
from src.knob_tuner.stages.models import (
    CompiledPlan,
    CompileRejection,
    ScreenVerdict,
)
from src.knob_tuner.sub_agents.db_inspector.models import DbInspectorOutput
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
            ],
            "resource_budget": {"cpu_cores": 4, "memory_gb": 8.0},
            "durability_profile": "strict",
            "max_set_knobs": 20,
        }
    )


def test_state_is_not_a_dict_and_not_copyable():
    """Pin the ADK invariant that caused the bug: dict(State) must not work."""
    st = State(value={"a": 1}, delta={})
    assert not isinstance(st, dict)
    assert not hasattr(st, "keys")
    with pytest.raises((KeyError, TypeError, AttributeError)):
        dict(st)
    # ...yet the helpers nodes.py relies on are all present.
    for attr in ("get", "setdefault", "__getitem__", "__setitem__", "update"):
        assert hasattr(st, attr), attr


def test_state_helper_returns_live_mapping():
    ctx = AdkCtx(validation_attempt_count=0)
    live = nodes._state(ctx)
    assert live is ctx.state
    live["validation_attempt_count"] = 7
    assert ctx.state["validation_attempt_count"] == 7


def test_state_helper_rejects_detached_copy():
    """A broken ctx must raise, never yield a disconnected {}.

    The old ``dict(state or {})`` fallback silently lost writes (attempt
    counter unreachable → runaway loop); failure visibility requires the
    loud TypeError instead.
    """

    class _BrokenCtx:
        state = None

    with pytest.raises(TypeError):
        nodes._state(_BrokenCtx())

    class _NoStateCtx:
        pass

    with pytest.raises(TypeError):
        nodes._state(_NoStateCtx())


def _fail_validate(**kwargs):
    """Mocked validate_fn: FAIL arm slightly below baseline."""
    return {
        "status": "FAIL",
        "paired": {
            "baseline": {"per_run_tps": [100.0, 101.0, 102.0]},
            "tuned": {"per_run_tps": [99.0, 98.5, 99.5]},
        },
        "reasons": ["regression"],
        "stopped_early": False,
    }


def _compiled_fail(exp_name="e1"):
    from src.knob_tuner.contracts import KnobPlan

    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    return CompiledPlan(
        plan=plan.model_dump(),
        exp_name=exp_name,
        phase="screen",
        valid_knobs=["work_mem"],
    )


def test_controller_attempt_persists_to_cap_with_real_state():
    """Attempts persist 1->2->3 via screen; controller never recounts.

    New loop per attempt: screen (records + bumps) -> diagnosis ->
    controller (pure route). Routes flip to done at cap with attempt_cap.
    """
    from src.knob_tuner.stages.models import DiagnosisOutput

    ctx = AdkCtx(max_attempts=3, min_improvement_pct=2.0)
    routes = []
    for i, expected in enumerate((1, 2, 3), start=1):
        verdict = nodes.screen_candidate(
            ctx, _compiled_fail(exp_name=f"e{i}"), validate_fn=_fail_validate
        )
        assert verdict.status == "FAIL"
        assert ctx.state["validation_attempt_count"] == expected  # persisted, not recomputed
        out = nodes.confirmation_controller(
            ctx,
            DiagnosisOutput(
                correction="drop_knob", targets=["work_mem"], rationale="bad", confidence=0.7
            ),
        )
        assert ctx.state["validation_attempt_count"] == expected  # controller never recounts
        assert len(ctx.state["experiment_history"]) == expected
        routes.append(out["route"])
    assert routes == ["retry", "retry", "done"]
    assert ctx.route == "done"
    assert out.get("reason") == "attempt_cap"
    assert len(ctx.state["experiment_history"]) == 3


def test_materialize_visible_to_compile_with_real_state(tmp_path):
    """knobs_info written by materialize must be visible to compile."""
    ctx = AdkCtx(knob_path=str(tmp_path))
    out = nodes.materialize_inventory(
        ctx,
        DbInspectorOutput(
            db_type="postgres",
            db_version="16.1",
            tables=[{"name": "t", "approximate_row_count": 10}],
            available_knobs=[
                {"name": "work_mem", "current_value": "4MB", "context": "user"},
            ],
        ),
    )
    assert out["n_knobs"] == 1
    # Write persisted on the live State (old bug: lost in disconnected {}).
    assert len(ctx.state.get("knobs_info") or []) == 1

    compiled = nodes.compile_candidate(
        ctx,
        {"name": "exp-1", "phase": "screen",
         "levels": [{"knob": "work_mem", "value": "64MB"}]},
    )
    assert isinstance(compiled, CompiledPlan), compiled
    assert "work_mem" in compiled.valid_knobs


def test_compile_rejects_unknown_knob_with_real_state():
    """Sanity: with live state, validation still rejects (not blanket-ok)."""
    ctx = AdkCtx()
    _seed_inventory(ctx)
    out = nodes.compile_candidate(
        ctx,
        {"name": "e", "phase": "screen",
         "levels": [{"knob": "nope", "value": "1"}]},
    )
    assert isinstance(out, CompileRejection)


def test_prepare_run_persists_max_attempts_with_real_state(tmp_path):
    ctx = AdkCtx(
        resource_budget={"cpu_cores": 4, "memory_gb": 8.0},
        max_attempts=4,
        run_id="r1",
        run_dir=str(tmp_path),
    )
    payload = nodes.prepare_run(ctx)
    assert payload["max_attempts"] == 4
    # Belt-and-braces: cap source explicit on state, not just run_config payload.
    assert ctx.state["max_attempts"] == 4
    assert ctx.state["run_config"]["max_attempts"] == 4

    # Controller honors the persisted cap: after 4 recorded screens the
    # next diagnosis routes done (attempt_cap), with no recounting.
    from src.knob_tuner.stages.models import DiagnosisOutput

    last_route = None
    for i in range(4):
        nodes.screen_candidate(
            ctx, _compiled_fail(exp_name=f"e{i}"), validate_fn=_fail_validate
        )
        out = nodes.confirmation_controller(
            ctx,
            DiagnosisOutput(
                correction="drop_knob", targets=["work_mem"], rationale="bad", confidence=0.7
            ),
        )
        last_route = out["route"]
    assert out["validation_attempt_count"] == 4
    assert last_route == "done"
    assert out.get("reason") == "attempt_cap"


def _pass12_validate(**kwargs):
    """Mocked validate_fn: PASS arm at ~+12% over baseline."""
    if kwargs.get("baseline_only"):
        return {
            "baseline": {"per_run_tps": [100.0, 101.0, 102.0, 100.5, 101.5]},
            "status": "ok",
        }
    return {
        "status": "PASS",
        "paired": {
            "baseline": {"per_run_tps": [100.0, 101.0, 102.0, 100.5, 101.5]},
            "tuned": {"per_run_tps": [112.0, 113.0, 114.0, 112.5, 113.5]},
        },
        "reasons": [],
        "stopped_early": False,
    }


def _compiled_pass12(exp_name="e-pass"):
    from src.knob_tuner.contracts import KnobPlan

    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    return CompiledPlan(
        plan=plan.model_dump(),
        exp_name=exp_name,
        phase="screen",
        valid_knobs=["work_mem"],
    )


def test_screen_to_decision_keeps_winner_plan_with_real_state():
    """Regression: 4 PASS arms at +12% must not yield knob_plan=null.

    Chains screen_candidate -> decision on a REAL ADK State and asserts the
    terminal decision carries a non-empty winner plan that bridges to
    production_preflight (not blocked).
    """
    ctx = AdkCtx(min_improvement_pct=2.0, durability_profile="strict")
    compiled = _compiled_pass12()
    verdict = nodes.screen_candidate(ctx, compiled, validate_fn=_pass12_validate)
    assert isinstance(verdict, ScreenVerdict)
    assert verdict.status == "PASS"
    assert verdict.mean_delta_pct > 10.0  # ~+12% arm

    # Lite row must retain the plan dump (heavy result artifacts stripped).
    rows = ctx.state.get("all_rows") or []
    assert len(rows) == 1
    assert isinstance(rows[0].get("plan"), dict)
    assert rows[0]["plan"].get("knobs")
    assert "result" not in rows[0]
    # Fallback source must be populated.
    cands = ctx.state.get("candidates") or []
    assert len(cands) == 1
    assert isinstance(cands[0].get("plan"), dict)
    assert cands[0].get("plan_hash") == rows[0].get("plan_hash")

    dec = nodes.decision(ctx)
    assert dec.decision in ("apply_winner", "keep_best"), dec.summary
    assert isinstance(dec.winner_plan, dict) and dec.winner_plan.get("knobs"), dec
    assert dec.winner_plan["knobs"][0]["name"] == "work_mem"

    # TerminalDecision bridges to knob_plan: preflight must not block.
    pre = nodes.production_preflight(ctx, dec)
    assert pre.route in ("auto", "maintenance_assisted"), pre
    bridged = nodes._extract_winner_plan(dec, nodes._state(ctx))
    assert bridged.get("knobs")


def test_decision_fallback_matches_by_plan_hash_with_real_state():
    """Legacy rows without plan must still resolve via candidates plan_hash."""
    from src.knob_tuner.contracts import KnobPlan

    plan_dump = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    ).model_dump()
    plan_hash = KnobPlan.model_validate(plan_dump).plan_hash()
    ctx = AdkCtx(
        min_improvement_pct=2.0,
        all_rows=[
            {
                "arm": "e-legacy",
                "phase": "screen",
                "plan_hash": plan_hash,
                "status": "PASS",
                "mean_delta_pct": 12.0,
                "lcb_pct": 11.0,
                "ucb_pct": 13.0,
                "df": 8.0,
                "confirmed": True,
                "improvement_confident": True,
                "paired": {"baseline": {}, "tuned": {}},
                "reasons": [],
                # no "plan"/"result": legacy stripped shape
            }
        ],
        candidates=[
            {
                "plan": plan_dump,
                "plan_hash": plan_hash,
                "result": {"paired": {"baseline": {}, "tuned": {}}},
            }
        ],
        baseline_tps=[100.0],
    )
    dec = nodes.decision(ctx)
    assert dec.decision in ("apply_winner", "keep_best"), dec.summary
    assert dec.winner_plan.get("knobs"), dec
