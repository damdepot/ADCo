"""Withheld-path audit lines for terminal decision()."""

from src.knob_tuner.contracts import KnobPlan
from src.knob_tuner.stages import nodes
from src.knob_tuner.stages.models import TerminalDecision
from tests.test_knob_tuner.conftest import FakeCtx


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


def test_withheld_mixed_rows_emit_per_row_audit_and_no_apply():
    ctx = FakeCtx({"min_improvement_pct": 2.0, "certify_lcb_pct": 5.0})
    fail_row = _row(mean=-1.0, lcb=-2.0, status="FAIL", confirmed=False, arm="e-fail")
    unconfirmed = _row(mean=8.0, lcb=6.0, status="PASS", confirmed=False, arm="e-unconf")
    below_bar = _row(mean=4.0, lcb=3.0, status="PASS", confirmed=True, arm="e-below")
    out = nodes.decision(
        ctx, all_rows=[fail_row, unconfirmed, below_bar], baseline_tps=[100.0]
    )
    assert isinstance(out, TerminalDecision)
    assert out.decision != "apply_winner"
    assert out.winner_plan == {}
    reasons = out.summary["reasons"]
    withheld = [r for r in reasons if r.startswith("withheld ")]
    # One audit line per considered row.
    assert len(withheld) == 3
    for name in ("e-fail", "e-unconf", "e-below"):
        assert any(r.startswith(f"withheld {name}:") for r in withheld)
    # Bar values surface in every line (certify bar 5.00, ranking gate 2.00).
    for r in withheld:
        assert "vs certify bar 5.00" in r
        assert "ranking gate 2.00" in r
    assert any("status=FAIL" in r for r in withheld)
    assert any("confirmed=False" in r for r in withheld)
    assert any("confirmed=True" in r for r in withheld)
    # Archive carries the certify threshold next to the ranking gate.
    archive = out.summary["archive"]
    assert float(archive["certify_threshold"]) == 5.0
    assert float(archive["min_improvement_pct"]) == 2.0


def test_applied_path_has_no_withheld_lines():
    ctx = FakeCtx({"min_improvement_pct": 2.0})
    rows = [_row(mean=8.0, lcb=6.0, arm="e-best"), _row(mean=5.0, lcb=3.0, arm="e-runner")]
    out = nodes.decision(ctx, all_rows=rows, baseline_tps=[100.0])
    assert out.decision == "apply_winner"
    assert out.summary["stats"]["mean_delta_pct"] == 8.0
    reasons = out.summary["reasons"]
    assert not any("withheld " in r for r in reasons)


def test_garbage_empty_rows_never_raise():
    # Empty rows with no pairing -> fail; with pairing -> inconclusive.
    out_fail = nodes.decision(FakeCtx({}), all_rows=[], baseline_tps=[])
    assert out_fail.decision == "fail"
    out_inc = nodes.decision(FakeCtx({}), all_rows=[], baseline_tps=[100.0])
    assert out_inc.decision == "inconclusive"
    # Garbage rows (non-dict, empty dict, unparseable lcb) must not raise.
    garbage = [{}, None, "junk", {"status": None, "lcb_pct": "oops", "arm": ""}]
    out = nodes.decision(FakeCtx({}), all_rows=garbage, baseline_tps=[100.0])
    assert out.decision in ("inconclusive", "fail")
    assert isinstance(out, TerminalDecision)
