"""Two-score STOP/NEXT gate tests with real ADK State (no live benchmarks).

Covers: p_win sanity (win/noise/loss, degenerate + insufficient inputs,
scipy t-CDF delegation), the controller gate matrix, state-overridable
thresholds, gate observability fields, screen-stored p_win, and the evidence
bundle P(win) column.
"""

import pytest

from src.knob_tuner.contracts import KnobPlan
from src.knob_tuner.stages import nodes
from src.knob_tuner.stages.evidence import build_evidence_bundle
from src.knob_tuner.stages.models import CompiledPlan, DiagnosisOutput
from src.knob_tuner.sub_agents.diagnosis_agent import prompt as diag_prompt_mod
from src.knob_tuner.tools.stats import p_win, welch_delta
from tests.test_knob_tuner.conftest import AdkCtx

BASE = [100.0, 101.0, 102.0, 100.5, 101.5]
WIN = [112.0, 113.0, 114.0, 112.5, 113.5]
LOSS = [88.0, 89.0, 90.0, 88.5, 89.5]
NOISE = [100.4, 100.8, 102.2, 99.9, 101.7]


def _paired(baseline=BASE, tuned=WIN):
    return {"baseline": {"per_run_tps": list(baseline)},
            "tuned": {"per_run_tps": list(tuned)}}


def _gate_state(last_row, **over):
    seed = {
        "max_attempts": 6,
        "validation_attempt_count": 1,
        "min_improvement_pct": 2.0,
        # Winner-stop tests predate the compounding quota; set the target to 1
        # so a single winner stop is permitted (quota-met).
        "success_candidates": 1,
        "experiment_history": [],
        "rejected_history": [],
        "last_screen_row": dict(last_row),
    }
    seed.update(over)
    return AdkCtx(**seed)


def _stop_diag(stop_reason, confidence):
    return DiagnosisOutput(correction="stop", targets=[], rationale=f"{stop_reason} claim",
                           confidence=confidence, stop_reason=stop_reason)


# --- p_win sanity ---


def test_p_win_clear_win_near_one():
    assert p_win(BASE, WIN) > 0.99


def test_p_win_pure_noise_near_half():
    assert 0.35 < p_win(BASE, NOISE) < 0.65


def test_p_win_clear_loss_near_zero():
    assert p_win(BASE, LOSS) < 0.01


def test_p_win_insufficient_evidence_is_none():
    assert p_win([100.0], [105.0]) is None
    assert p_win([], []) is None
    assert p_win([0.0, 0.0], [10.0, 12.0]) is None
    assert p_win(None, None) is None
    assert p_win(["x", None], [1.0, 2.0]) is None


def test_p_win_degenerate_zero_spread_follows_sign():
    assert p_win([100.0, 100.0, 100.0], [105.0, 105.0, 105.0]) == 1.0
    assert p_win([100.0, 100.0, 100.0], [95.0, 95.0, 95.0]) == 0.0
    # An identical constant pair carries no evidence (not a 0.5 toss-up).
    assert p_win([100.0, 100.0], [100.0, 100.0]) is None


def test_p_win_delegates_to_scipy_t_cdf(monkeypatch):
    import scipy.stats as _scipy_stats

    calls = []
    real_cdf = _scipy_stats.t.cdf

    def _spy(t_stat, df):
        calls.append((t_stat, df))
        return real_cdf(t_stat, df)

    monkeypatch.setattr(_scipy_stats.t, "cdf", _spy)
    assert p_win(BASE, WIN) > 0.99
    assert calls, "p_win must delegate to scipy.stats.t.cdf"


def test_p_win_matches_scipy_reference_values():
    from scipy.stats import t as _t

    for baseline, tuned in ((BASE, WIN), (BASE, NOISE), (BASE, LOSS)):
        stats = welch_delta(list(baseline), list(tuned))
        expected = float(
            _t.cdf(stats["mean_delta_pct"] / stats["se_pct"], stats["df"])
        )
        assert p_win(baseline, tuned) == pytest.approx(expected, rel=1e-12)


# --- gate matrix ---


def _win_row():
    return {"arm": "e-win", "phase": "screen", "status": "PASS",
            "mean_delta_pct": 12.0, "lcb_pct": 11.0, "ucb_pct": 13.0,
            "confirmed": True, "reasons": [], "paired": _paired()}


def _loss_row():
    return {"arm": "e-loss", "phase": "screen", "status": "FAIL",
            "mean_delta_pct": -11.0, "lcb_pct": -12.0, "ucb_pct": -10.0,
            "confirmed": False, "reasons": ["regression"], "paired": _paired(tuned=LOSS)}


def _modest_win_row():
    # Small +1% win: P(win) ~0.9 (backs a winner-claim) but lcb < 2%, so the
    # confident-win backstop stays silent and gate behavior is isolated.
    return {"arm": "e-modest", "phase": "screen", "status": "PASS",
            "mean_delta_pct": 1.0, "lcb_pct": -0.7, "ucb_pct": 2.7,
            "confirmed": True, "reasons": [],
            "paired": _paired(baseline=[100.0, 102.0, 99.0, 101.0, 100.0],
                              tuned=[101.0, 103.0, 100.0, 102.0, 101.0])}


def test_gate_agree_winner_stops():
    cleared = {"name": "e0", "phase": "screen", "n_knobs": 1,
               "mean_delta_pct": 12.0, "lcb_pct": 11.0, "status": "PASS",
               "confirmed": True}
    ctx = _gate_state(_win_row(), experiment_history=[cleared])
    out = nodes.confirmation_controller(ctx, _stop_diag("winner", 0.9))
    assert out["route"] == "done" and ctx.route == "done"
    assert out["reason"] == "stopped_by_diagnosis"
    assert out["gate"] == "stop_agree_winner"
    assert out["diag_confidence"] == 0.9
    assert out["stat_p_win"] is not None and out["stat_p_win"] >= 0.7
    assert out["thresholds"] == {"stop_diag_min": 0.6, "stop_stat_win_min": 0.7,
                                "futility_stat_max": 0.4}


def test_gate_agree_futility_stops():
    ctx = _gate_state(_loss_row())
    out = nodes.confirmation_controller(ctx, _stop_diag("futility", 0.8))
    assert out["route"] == "done"
    assert out["reason"] == "stopped_by_diagnosis"
    assert out["gate"] == "stop_agree_futility"
    assert out["stat_p_win"] is not None and out["stat_p_win"] <= 0.4


def test_gate_winner_claim_with_low_stats_continues():
    ctx = _gate_state(_loss_row())
    out = nodes.confirmation_controller(ctx, _stop_diag("winner", 0.9))
    assert out["route"] == "retry"
    assert out["reason"] == "diag_stat_disagree"
    assert out["diag_confidence"] == 0.9
    assert out["stat_p_win"] is not None and out["stat_p_win"] < 0.7
    assert out["gate"] == "disagree"


def test_gate_low_confidence_stop_continues():
    ctx = _gate_state(_modest_win_row())
    out = nodes.confirmation_controller(ctx, _stop_diag("winner", 0.5))
    assert out["route"] == "retry"
    assert out["reason"] == "diag_stat_disagree"
    assert out["gate"] == "disagree"
    ctx2 = _gate_state(_loss_row())
    out2 = nodes.confirmation_controller(ctx2, _stop_diag("futility", 0.5))
    assert out2["route"] == "retry"
    assert out2["reason"] == "diag_stat_disagree"


def test_gate_futility_claim_with_high_p_win_continues():
    # Small positive delta: high P(win) but lcb below min_improvement, so the
    # confident-win backstop must not fire either — the loop continues.
    row = {"arm": "e-toss", "phase": "screen", "status": "PASS",
           "mean_delta_pct": 1.0, "lcb_pct": -0.2, "ucb_pct": 2.2,
           "confirmed": True, "reasons": [],
           "paired": _paired(baseline=[100.0, 102.0, 99.0, 101.0, 100.0],
                             tuned=[101.0, 103.0, 100.0, 102.0, 101.0])}
    ctx = _gate_state(row)
    out = nodes.confirmation_controller(ctx, _stop_diag("futility", 0.9))
    assert out["stat_p_win"] is not None and out["stat_p_win"] > 0.4
    assert out["route"] == "retry"
    assert out["reason"] == "diag_stat_disagree"


def test_gate_thresholds_overridable_via_state():
    # A cleared history row so the winner quota (target=1) is met; the test
    # isolates the stop_diag_min override, not quota behavior.
    cleared = {"name": "e0", "phase": "screen", "n_knobs": 1,
               "mean_delta_pct": 12.0, "lcb_pct": 11.0, "status": "PASS",
               "confirmed": True}
    ctx = _gate_state(_modest_win_row(), stop_diag_min=0.99, experiment_history=[cleared])
    out = nodes.confirmation_controller(ctx, _stop_diag("winner", 0.9))
    assert out["route"] == "retry"  # 0.9 < overridden 0.99
    ctx = _gate_state(_modest_win_row(), stop_diag_min=0.5, experiment_history=[cleared])
    out = nodes.confirmation_controller(ctx, _stop_diag("winner", 0.6))
    assert out["route"] == "done"  # 0.6 >= overridden 0.5
    ctx = _gate_state(_modest_win_row(), stop_stat_win_min=0.99999, experiment_history=[cleared])
    out = nodes.confirmation_controller(ctx, _stop_diag("winner", 0.95))
    assert out["route"] == "retry"
    assert out["thresholds"]["stop_stat_win_min"] == 0.99999
    ctx = _gate_state(_loss_row(), futility_stat_max=1.0)
    out = nodes.confirmation_controller(ctx, _stop_diag("futility", 0.9))
    assert out["route"] == "done"
    assert out["gate"] == "stop_agree_futility"


def test_gate_disagree_still_honors_attempt_cap():
    ctx = _gate_state(_loss_row(), validation_attempt_count=6)
    out = nodes.confirmation_controller(ctx, _stop_diag("winner", 0.9))
    assert out["route"] == "done"
    assert out["reason"] == "attempt_cap"


def test_gate_uses_best_confirmed_row():
    weak = {"arm": "e-weak", "phase": "screen", "status": "PASS",
            "mean_delta_pct": 1.0, "lcb_pct": -1.0, "ucb_pct": 3.0,
            "confirmed": True, "reasons": [],
            "paired": _paired(tuned=[101.0, 99.0, 102.0, 100.0, 101.0])}
    strong = dict(_win_row(), arm="e-strong")
    cleared = {"name": "e0", "phase": "screen", "n_knobs": 1,
               "mean_delta_pct": 12.0, "lcb_pct": 11.0, "status": "PASS",
               "confirmed": True}
    ctx = _gate_state(_loss_row(), all_rows=[weak, strong],
                      experiment_history=[cleared])
    out = nodes.confirmation_controller(ctx, _stop_diag("winner", 0.9))
    assert out["route"] == "done"
    assert out["stat_p_win"] is not None and out["stat_p_win"] > 0.9


def test_gate_observability_fields_present_on_stop():
    ctx = _gate_state(_win_row())
    out = nodes.confirmation_controller(ctx, _stop_diag("winner", 0.9))
    for key in ("diag_confidence", "stat_p_win", "stop_reason", "gate", "thresholds"):
        assert key in out, f"missing observability key {key!r}"
    assert out["stop_reason"] == "winner"


# --- screen stores p_win; bundle shows P(win) ---


def _compiled(exp_name="e1"):
    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    return CompiledPlan(plan=plan.model_dump(), exp_name=exp_name, phase="screen",
                        valid_knobs=["work_mem"])


def test_screen_row_carries_p_win():
    ctx = AdkCtx(shared_baseline={"per_run_tps": list(BASE)},
                 baseline_cache_key=nodes._baseline_cache_key(None, None),
                 min_improvement_pct=2.0, max_attempts=6)

    def _validate(**kwargs):
        return {"status": "PASS", "paired": _paired(), "reasons": [],
                "stopped_early": False}

    verdict = nodes.screen_candidate(ctx, _compiled(), validate_fn=_validate)
    assert verdict.status == "PASS"
    rows = ctx.state.get("experiment_history")
    assert len(rows) == 1
    assert rows[0]["p_win"] is not None and rows[0]["p_win"] > 0.9


def test_evidence_bundle_shows_p_win_column():
    state = {
        "experiment_history": [
            {"name": "e1", "phase": "screen", "n_knobs": 1, "mean_delta_pct": 12.0,
             "lcb_pct": 11.0, "status": "PASS", "confirmed": True, "p_win": 0.99},
            {"name": "e0", "phase": "screen", "n_knobs": 1, "mean_delta_pct": -1.0,
             "lcb_pct": -2.0, "status": "FAIL", "confirmed": False},
        ],
        "last_screen_row": dict(_win_row(), arm="e1"),
        "rejected_history": [],
        "resource_budget": {"cpu": 4, "memory_gb": 8},
        "validation_attempt_count": 2,
        "max_attempts": 6,
    }
    text = build_evidence_bundle(state)
    assert "p(win)" in text
    assert "0.99" in text  # stored history value
    assert "n/a" in text  # row without p_win
    assert "Last verdict" in text


# --- prompt calibration + stop_reason ---


def _next_diag():
    return DiagnosisOutput(correction="drop_knob", targets=[], rationale="keep screening",
                           confidence=0.6)


def test_confident_win_continues_and_records_incumbent():
    # Quota unmet (empty history, target=1): a confident win keeps
    # collecting and banks the leading arm; done happens at quota, the cap,
    # or on a diagnosis stop.
    row = dict(_win_row(), plan_hash="hash-aaa")
    ctx = _gate_state(row)
    out = nodes.confirmation_controller(ctx, _next_diag())
    assert out["route"] == "retry"
    assert out["reason"].startswith("collecting_success_candidates")
    assert "hash-aaa" in out["reason"] or "e-win" in out["reason"]
    incumbent = ctx.state.get("incumbent")
    assert incumbent == {"plan_hash": "hash-aaa", "mean": 12.0, "lcb": 11.0}
    assert out["incumbent"] == incumbent


def test_confident_win_at_cap_stops():
    row = dict(_win_row(), plan_hash="hash-aaa")
    ctx = _gate_state(row, validation_attempt_count=6)
    out = nodes.confirmation_controller(ctx, _next_diag())
    assert out["route"] == "done"
    assert out["reason"] == "attempt_cap"


def test_first_win_then_better_arm_keeps_later_incumbent():
    first = dict(_win_row(), arm="e-first", plan_hash="hash-aaa",
                 mean_delta_pct=12.0, lcb_pct=11.0)
    ctx = _gate_state(first)
    out1 = nodes.confirmation_controller(ctx, _next_diag())
    assert out1["route"] == "retry"
    assert out1["reason"].startswith("collecting_success_candidates")
    assert ctx.state.get("incumbent") == {"plan_hash": "hash-aaa", "mean": 12.0, "lcb": 11.0}
    # A strictly better (mean, lcb) verdict replaces the incumbent.
    better = dict(_win_row(), arm="e-better", plan_hash="hash-bbb",
                  mean_delta_pct=15.0, lcb_pct=12.0)
    ctx.state["last_screen_row"] = dict(better)
    out2 = nodes.confirmation_controller(ctx, _next_diag())
    assert out2["route"] == "retry"
    assert out2["reason"].startswith("collecting_success_candidates")
    assert ctx.state.get("incumbent") == {"plan_hash": "hash-bbb", "mean": 15.0, "lcb": 12.0}
    assert "hash-bbb" in out2["reason"] or "e-better" in out2["reason"]
    # A worse verdict must not displace the leader.
    worse = dict(_win_row(), arm="e-worse", plan_hash="hash-ccc",
                 mean_delta_pct=10.0, lcb_pct=9.0)
    ctx.state["last_screen_row"] = dict(worse)
    out3 = nodes.confirmation_controller(ctx, _next_diag())
    assert out3["route"] == "retry"
    assert out3["reason"].startswith("collecting_success_candidates")
    assert ctx.state.get("incumbent") == {"plan_hash": "hash-bbb", "mean": 15.0, "lcb": 12.0}


def test_diagnosis_prompt_has_calibration_section():
    text = diag_prompt_mod.DIAGNOSIS_AGENT_PROMPT
    assert "0.9+" in text
    assert "0.5-0.7" in text
    assert "Never 1.0" in text
    assert "confidence" in text


def test_diagnosis_prompt_instructs_stop_reason():
    text = diag_prompt_mod.DIAGNOSIS_AGENT_PROMPT
    assert "stop_reason" in text
    assert '"winner"' in text
    assert '"futility"' in text


def test_diagnosis_prompt_keeps_strategy_only_prohibitions():
    text = diag_prompt_mod.DIAGNOSIS_AGENT_PROMPT
    assert "NEVER emit knob values" in text
    assert "NEVER emit run verdicts" in text
    assert "NEVER persist" in text


# --- compounding quota (success candidates) ---


def _cleared(name="c", lcb=11.0):
    return {"name": name, "phase": "screen", "n_knobs": 1,
            "mean_delta_pct": lcb - 1.0, "lcb_pct": lcb, "status": "PASS",
            "confirmed": True}


def test_quota_unmet_downgrades_winner_stop_to_collecting():
    # Two cleared rows remain short of a target of 3: the winner stop must not
    # end the campaign; the route becomes a quota-collecting retry.
    ctx = _gate_state(
        _win_row(),
        success_candidates=3,
        experiment_history=[_cleared("c1"), _cleared("c2")],
    )
    out = nodes.confirmation_controller(ctx, _stop_diag("winner", 0.9))
    assert out["route"] == "retry"
    assert out["reason"] == "quota_not_met"
    assert out["gate"] == "quota_not_met"
    assert out["success_candidates"] == {
        "found": 2, "target": 3, "min_improvement_pct": 2.0
    }


def test_quota_met_allows_winner_stop():
    ctx = _gate_state(
        _win_row(),
        success_candidates=2,
        experiment_history=[_cleared("c1"), _cleared("c2")],
    )
    out = nodes.confirmation_controller(ctx, _stop_diag("winner", 0.9))
    assert out["route"] == "done"
    assert out["reason"] == "stopped_by_diagnosis"
    assert out["success_candidates"]["found"] == 2


def test_quota_unmet_but_futility_still_stops():
    # A futility stop is a dead campaign: it ends even while the quota is
    # unmet (otherwise the loop spins forever on a hopeless target).
    cleared = _cleared("c1")
    ctx = _gate_state(_loss_row(), success_candidates=3,
                      experiment_history=[cleared])
    out = nodes.confirmation_controller(ctx, _stop_diag("futility", 0.8))
    assert out["route"] == "done"
    assert out["reason"] == "stopped_by_diagnosis"
    assert out["gate"] == "stop_agree_futility"


def test_quota_unmet_still_honors_attempt_cap():
    # While collecting, the attempt cap MUST remain reachable or the loop can
    # never exit. A cleared-but-short row + cap reached → done(attempt_cap).
    ctx = _gate_state(
        _win_row(),
        success_candidates=5,
        validation_attempt_count=6,
        experiment_history=[_cleared("c1")],
    )
    out = nodes.confirmation_controller(ctx, _stop_diag("winner", 0.9))
    assert out["route"] == "done"
    assert out["reason"] == "attempt_cap"


def test_quota_met_backstop_applies_without_stop_diagnosis():
    # No stop diagnosis: a latest confident win ends the campaign once the
    # quota target is met.
    ctx = _gate_state(
        _win_row(),
        success_candidates=1,
        experiment_history=[_cleared("c1")],
    )
    out = nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="drop_knob", targets=["work_mem"],
                        rationale="next", confidence=0.6),
    )
    assert out["route"] == "done"
    assert out["reason"] == "confident_win_backstop"


def test_quota_unmet_backstop_collects_and_caps_later():
    # A confident win with the quota unmet collects; the cap later ends it.
    ctx = _gate_state(
        _win_row(),
        success_candidates=4,
        validation_attempt_count=3,
        experiment_history=[_cleared("c1")],
    )
    out = nodes.confirmation_controller(
        ctx,
        DiagnosisOutput(correction="drop_knob", targets=["work_mem"],
                        rationale="next", confidence=0.6),
    )
    assert out["route"] == "retry"
    assert out["reason"].startswith("collecting_success_candidates")


def test_get_success_candidates_defaults_and_floor():
    from src.knob_tuner.contracts import (
        DEFAULT_SUCCESS_CANDIDATES,
        get_success_candidates,
    )

    assert get_success_candidates({}) == DEFAULT_SUCCESS_CANDIDATES
    assert get_success_candidates({"success_candidates": 0}) == DEFAULT_SUCCESS_CANDIDATES
    assert get_success_candidates({"success_candidates": None}) == DEFAULT_SUCCESS_CANDIDATES
    assert get_success_candidates({"success_candidates": 3}) == 3
    assert get_success_candidates({"success_candidates": "7"}) == 7
    assert get_success_candidates({"success_candidates": -5}) == 1
