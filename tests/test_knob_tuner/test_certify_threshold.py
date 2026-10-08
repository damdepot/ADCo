"""Certify-threshold (promotion bar) tests for the knob-tuner controller.

Certification uses ``certify_lcb_pct`` (default 1.0, explicit 0.0 honored);
the ranking/display gate (``min_improvement_pct``) no longer decides it.
Hermetic: plain-dict state plus real ADK State, no DB.
"""

from src.knob_tuner.contracts import DEFAULT_CERTIFY_LCB_PCT, get_certify_lcb_pct
from src.knob_tuner.stages import nodes
from src.knob_tuner.stages.models import DiagnosisOutput
from src.knob_tuner.stages.nodes import stats_coercion as sc
from src.knob_tuner.stages.nodes._common import _certify_lcb_pct
from tests.test_knob_tuner.conftest import AdkCtx


def _verdict_row(mean=2.5, lcb=1.39, plan_hash="hash-cert"):
    return {"arm": "e-cert", "phase": "screen", "status": "PASS",
            "mean_delta_pct": mean, "lcb_pct": lcb, "ucb_pct": lcb + 1.0,
            "confirmed": True, "reasons": [], "plan_hash": plan_hash}


def _gate_state(last_row, **over):
    seed = {
        "max_attempts": 6,
        "max_winners": 1,
        "validation_attempt_count": 1,
        "min_improvement_pct": 5.0,
        "success_candidates": 1,
        "experiment_history": [],
        "rejected_history": [],
        "last_screen_row": dict(last_row),
    }
    seed.update(over)
    return AdkCtx(**seed)


def _next_diag():
    return DiagnosisOutput(correction="drop_knob", targets=[], rationale="keep screening",
                           confidence=0.6)


# --- reader semantics (default 1.0; explicit 0.0 honored) ---


def test_certify_reader_defaults_to_one():
    assert DEFAULT_CERTIFY_LCB_PCT == 1.0
    assert get_certify_lcb_pct({}) == 1.0
    assert _certify_lcb_pct({}) == 1.0
    assert get_certify_lcb_pct({"certify_lcb_pct": None}) == 1.0


def test_certify_reader_honors_explicit_zero():
    assert get_certify_lcb_pct({"certify_lcb_pct": 0.0}) == 0.0
    assert _certify_lcb_pct({"certify_lcb_pct": 0.0}) == 0.0


def test_certify_reader_honors_explicit_value():
    assert get_certify_lcb_pct({"certify_lcb_pct": 5.0}) == 5.0
    assert _certify_lcb_pct({"certify_lcb_pct": 5.0}) == 5.0


# --- (a)/(b): legacy entries derive certified from the certify bar ---


def test_small_confirmed_win_certifies_under_default():
    # (a) LCB +1.39 clears the 1.0 default even though it misses a strict
    # ranking gate.
    gate = _certify_lcb_pct({"min_improvement_pct": 5.0})
    assert gate == 1.0
    assert sc._is_entry_certified({"lcb": 1.39}, gate) is True


def test_negative_lcb_does_not_certify():
    # (b) LCB -0.05 never clears the bar.
    gate = _certify_lcb_pct({})
    assert sc._is_entry_certified({"lcb": -0.05}, gate) is False


def test_explicit_certified_flag_still_trusted():
    gate = _certify_lcb_pct({})
    assert sc._is_entry_certified({"lcb": -0.05, "certified": True}, gate) is True
    assert sc._is_entry_certified({"lcb": 11.0, "certified": False}, gate) is False


# --- (c): explicit certify_lcb_pct=5.0 restores old strict behavior ---


def test_explicit_strict_bar_restores_old_behavior():
    gate = _certify_lcb_pct({"certify_lcb_pct": 5.0})
    assert sc._is_entry_certified({"lcb": 1.39}, gate) is False
    assert sc._is_entry_certified({"lcb": 11.0}, gate) is True


def test_count_certified_winners_uses_certify_bar():
    winners = [{"plan_hash": "h1", "lcb": 1.39},
               {"plan_hash": "h2", "lcb": 11.0},
               {"plan_hash": "h3", "lcb": -0.05}]
    assert sc._count_certified_winners({"winners": winners}) == 2
    strict = {"winners": winners, "certify_lcb_pct": 5.0}
    assert sc._count_certified_winners(strict) == 1


# --- (d): _register_winner entry certified flag follows the new bar ---


def test_register_winner_certifies_small_win_under_default():
    state = {"last_screen_row": _verdict_row()}
    winners = sc._register_winner(state, {}, 2.5, 1.39)
    assert len(winners) == 1
    assert winners[0]["plan_hash"] == "hash-cert"
    assert winners[0]["certified"] is True


def test_register_winner_rejects_negative_lcb():
    state = {"last_screen_row": _verdict_row(lcb=-0.05, plan_hash="hash-neg")}
    winners = sc._register_winner(state, {}, 1.0, -0.05)
    assert len(winners) == 1
    assert winners[0]["certified"] is False


def test_register_winner_strict_bar_withholds_small_win():
    state = {"last_screen_row": _verdict_row(), "certify_lcb_pct": 5.0}
    winners = sc._register_winner(state, {}, 2.5, 1.39)
    assert len(winners) == 1
    assert winners[0]["certified"] is False


def test_register_winner_ignores_ranking_gate():
    # A strict ranking gate alone must NOT withhold certification.
    state = {"last_screen_row": _verdict_row(), "min_improvement_pct": 5.0}
    winners = sc._register_winner(state, {}, 2.5, 1.39)
    assert winners[0]["certified"] is True


# --- confident-win backstop fires on the certify bar ---


def test_backstop_fires_below_ranking_gate_under_default():
    cleared = {"name": "c1", "phase": "screen", "n_knobs": 1,
               "mean_delta_pct": 10.0, "lcb_pct": 11.0, "status": "PASS",
               "confirmed": True}
    ctx = _gate_state(_verdict_row(), experiment_history=[cleared])
    out = nodes.confirmation_controller(ctx, _next_diag())
    assert out["route"] == "done"
    assert out["reason"] == "confident_win_backstop"


def test_backstop_silent_below_strict_bar():
    cleared = {"name": "c1", "phase": "screen", "n_knobs": 1,
               "mean_delta_pct": 10.0, "lcb_pct": 11.0, "status": "PASS",
               "confirmed": True}
    ctx = _gate_state(_verdict_row(), certify_lcb_pct=5.0,
                      experiment_history=[cleared])
    out = nodes.confirmation_controller(ctx, _next_diag())
    assert out["route"] == "done"
    assert out["reason"] == "quota_met"
