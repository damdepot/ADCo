"""Wave 1 stage-model tests: schemas only, no wiring."""

import pytest
from pydantic import ValidationError

from src.knob_tuner.stages.models import (
    CandidateProposal,
    CompiledPlan,
    CompileRejection,
    CorrectionType,
    DiagnosisOutput,
    PreflightVerdict,
    ScreenVerdict,
    TerminalDecision,
    normalize_workload_profile,
)


def _levels():
    return [{"knob": "shared_buffers", "value": "256MB"}]


def test_candidate_requires_levels_missing():
    with pytest.raises(ValidationError):
        CandidateProposal(name="exp-1", phase="screen")  # type: ignore[call-arg]


def test_candidate_rejects_empty_levels():
    with pytest.raises(ValidationError):
        CandidateProposal(name="exp-1", phase="screen", levels=[])


def test_phase_validator_case_insensitive_ok():
    p = CandidateProposal(name="exp-1", phase="Screen", levels=_levels())
    assert p.phase == "screen"
    assert p.repaired is False
    assert p.phase_raw == ""
    p2 = CandidateProposal(name="exp-2", phase="  REFINEMENT ", levels=_levels())
    assert p2.phase == "refinement"
    assert p2.repaired is False
    assert p2.phase_raw == ""


def test_phase_validator_bad_phase_repairs():
    p = CandidateProposal(name="exp-1", phase="explore", levels=_levels())
    assert p.phase == "screen"
    assert p.repaired is True
    assert p.phase_raw == "explore"


def test_phase_leak_shared_buffers_repairs_and_compiles():
    # Live crash value: LLM put a knob name in the phase field, ADK
    # output_schema validation raised and killed the whole Workflow.
    p = CandidateProposal(
        name="exp-leak", phase="shared_buffers", levels=_levels()
    )
    assert p.phase == "screen"
    assert p.repaired is True
    assert p.phase_raw == "shared_buffers"
    from tests.test_knob_tuner.conftest import FakeCtx

    from src.knob_tuner.stages import nodes as stage_nodes

    ctx = FakeCtx(
        {
            "knobs_info": [
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
        }
    )
    out = stage_nodes.compile_candidate(ctx, p)
    assert not isinstance(out, ValidationError)
    assert getattr(out, "phase", "screen") == "screen"


def test_correction_type_values():
    assert {c.value for c in CorrectionType} == {
        "shrink_set",
        "change_phase",
        "drop_knob",
        "adjust_value",
        "retry_same",
        "stop",
    }
    d = DiagnosisOutput(correction="stop", confidence=0.9, stop_reason="winner")
    assert d.correction is CorrectionType.STOP
    assert d.targets == []
    assert d.confidence == 0.9
    assert d.stop_reason == "winner"
    with pytest.raises(ValidationError):
        DiagnosisOutput(correction="bogus", confidence=0.5)


def test_diagnosis_confidence_validation():
    with pytest.raises(ValidationError):
        DiagnosisOutput(correction="stop")  # confidence is required
    for bad in (-0.1, 1.5, float("nan"), "high", None, True):
        with pytest.raises(ValidationError):
            DiagnosisOutput(correction="retry_same", confidence=bad)
    for ok in (0.0, 0.5, 1.0):
        assert DiagnosisOutput(correction="retry_same", confidence=ok).confidence == ok


def test_diagnosis_stop_reason_defaults_and_validation():
    assert DiagnosisOutput(correction="stop", confidence=0.8).stop_reason == "futility"
    assert DiagnosisOutput(correction="stop", confidence=0.8, stop_reason="").stop_reason == "futility"
    assert DiagnosisOutput(correction="stop", confidence=0.8, stop_reason="WINNER").stop_reason == "winner"
    with pytest.raises(ValidationError):
        DiagnosisOutput(correction="stop", confidence=0.8, stop_reason="maybe")


def test_normalize_none_input():
    assert normalize_workload_profile(None, "") == {}
    out = normalize_workload_profile(None, "oltp nightly")
    assert out == {"workload_hint": "oltp nightly"}


def test_normalize_garbage_input_never_throws():
    out = normalize_workload_profile("not-a-dict", "hint")  # type: ignore[arg-type]
    assert out == {"workload_hint": "hint"}
    out = normalize_workload_profile([1, 2], "")  # type: ignore[arg-type]
    assert out == {}
    out = normalize_workload_profile({"query_types": ["SELECT"]}, None)  # type: ignore[arg-type]
    assert out["query_types"] == ["SELECT"]


def test_normalize_valid_input_merges_hint():
    info = {
        "query_types": ["SELECT", "UPDATE"],
        "orm_detected": "SQLAlchemy",
        "transaction_pattern": "explicit",
        "estimated_read_write_ratio": "80/20",
        "notable_patterns": ["pooling"],
    }
    out = normalize_workload_profile(info, "read-heavy")
    for key, val in info.items():
        assert out[key] == val
    assert out["workload_hint"] == "read-heavy"


def test_normalize_unwraps_nested_workload():
    out = normalize_workload_profile({"workload": {"orm_detected": "Django"}}, "h")
    assert out["orm_detected"] == "Django"
    assert out["workload_hint"] == "h"


def test_other_models_defaults():
    c = CompiledPlan(plan={"knobs": []}, exp_name="e", phase="screen")
    assert c.valid_knobs == []
    r = CompileRejection(reason="bad")
    assert r.errors == [] and r.design_name == ""
    s = ScreenVerdict(status="PASS")
    assert s.mean_delta_pct == 0.0 and s.paired == {} and s.reasons == []
    t = TerminalDecision(decision="fail")
    assert t.winner_plan == {}
    with pytest.raises(ValidationError):
        TerminalDecision(decision="bogus")
    v = PreflightVerdict(route="auto")
    assert v.reason == ""
    with pytest.raises(ValidationError):
        PreflightVerdict(route="bogus")
