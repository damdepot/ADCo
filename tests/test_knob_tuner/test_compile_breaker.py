"""Compile breaker + knob-name change_phase guard (hermetic, real ADK State)."""

from src.knob_tuner.stages import nodes
from src.knob_tuner.stages.models import (
    CompiledPlan,
    CompileRejection,
    DiagnosisOutput,
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


def test_change_phase_knob_name_target_does_not_reject():
    ctx = _ctx()
    _diagnose(ctx, "change_phase", ["work_mem"])
    result = nodes.compile_candidate(ctx, _proposal(phase="screen"))
    assert isinstance(result, CompiledPlan)


def test_change_phase_valid_target_still_rejects_mismatch():
    ctx = _ctx()
    _diagnose(ctx, "change_phase", ["refinement"])
    bad = nodes.compile_candidate(ctx, _proposal(phase="screen"))
    assert isinstance(bad, CompileRejection)
    assert "change_phase" in bad.reason
    assert "valid" in bad.reason.lower()
    assert any("valid" in str(e).lower() for e in (bad.errors or []))
    good = nodes.compile_candidate(ctx, _proposal(name="ok", phase="refinement"))
    assert isinstance(good, CompiledPlan)


def test_three_identical_rejections_downgrade_and_reset():
    ctx = _ctx()
    _diagnose(ctx, "drop_knob", ["work_mem"])
    reasons = []
    for i in range(3):
        rejected = nodes.compile_candidate(
            ctx, _proposal(name=f"dup-{i}", levels=[{"knob": "work_mem", "value": "64MB"}])
        )
        assert isinstance(rejected, CompileRejection)
        reasons.append(rejected.reason)
    assert reasons[0] == reasons[1] == reasons[2]
    assert ctx.state.get("consecutive_rejection_count") == 0
    assert ctx.state.get("consecutive_rejection_sig") == ""
    hist = ctx.state.get("diagnosis_history")
    assert hist and hist[-1]["correction"] == "retry_same"


def test_counter_resets_on_success_and_on_differing_reason():
    # Reset on success.
    ctx = _ctx()
    bad = nodes.compile_candidate(ctx, {"name": "empty", "phase": "screen", "levels": []})
    assert isinstance(bad, CompileRejection)
    assert int(ctx.state.get("consecutive_rejection_count") or 0) == 1
    good = nodes.compile_candidate(ctx, _proposal(name="ok"))
    assert isinstance(good, CompiledPlan)
    assert int(ctx.state.get("consecutive_rejection_count") or 0) == 0
    assert (ctx.state.get("consecutive_rejection_sig") or "") == ""

    # Reset on differing reason: two distinct rejection reasons.
    ctx2 = _ctx()
    first = nodes.compile_candidate(
        ctx2, {"name": "empty", "phase": "screen", "levels": []}
    )
    assert isinstance(first, CompileRejection)
    assert int(ctx2.state.get("consecutive_rejection_count") or 0) == 1
    second = nodes.compile_candidate(
        ctx2, {"name": "weird", "phase": "nope-phase", "levels": [{"knob": "work_mem", "value": "64MB"}]}
    )
    assert isinstance(second, CompileRejection)
    assert second.reason != first.reason
    assert int(ctx2.state.get("consecutive_rejection_count") or 0) == 1
    assert ctx2.state.get("consecutive_rejection_sig") == second.reason
