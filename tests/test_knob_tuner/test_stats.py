"""Unit tests for the knob_tuner Welch statistics helpers."""

import pytest

from src.knob_tuner.tools.stats import (
    estimate_multi_fidelity,
    futility_stop,
    t_crit,
    welch_delta,
)


def test_welch_delta_reports_sample_counts_and_sufficiency():
    stats = welch_delta([100.0, 110.0, 90.0], [130.0, 140.0, 120.0])
    assert stats["n1"] == 3 and stats["n2"] == 3
    assert stats["sufficient"] is True
    assert stats["zero_spread"] is False


def test_welch_delta_noisy_interval_brackets_mean():
    stats = welch_delta([100.0, 110.0, 90.0], [130.0, 140.0, 120.0])
    assert stats["se_pct"] > 0
    assert stats["df"] > 1
    assert stats["lcb_pct"] < stats["mean_delta_pct"] < stats["ucb_pct"]


def test_welch_delta_insufficient_samples_is_distinguishable():
    stats = welch_delta([100.0], [105.0])
    assert stats["sufficient"] is False
    assert stats["n1"] == 1 and stats["n2"] == 1
    assert stats["df"] == 0.0
    assert stats["lcb_pct"] == stats["ucb_pct"] == 0.0


def test_welch_delta_non_positive_baseline_is_insufficient():
    stats = welch_delta([0.0, 0.0], [10.0, 12.0])
    assert stats["sufficient"] is False
    assert stats["df"] == 0.0


def test_welch_delta_zero_spread_nonzero_delta_caps_df():
    stats = welch_delta([100.0, 100.0, 100.0], [105.0, 105.0, 105.0])
    assert stats["zero_spread"] is True
    assert stats["df"] == 1.0
    assert stats["t_crit"] == 12.706
    assert stats["mean_delta_pct"] == pytest.approx(5.0)
    assert stats["lcb_pct"] == stats["ucb_pct"] == pytest.approx(5.0)


def test_welch_delta_zero_spread_zero_delta_has_no_evidence():
    stats = welch_delta([100.0, 100.0], [100.0, 100.0])
    assert stats["zero_spread"] is True
    assert stats["df"] == 0.0
    assert stats["lcb_pct"] == 0.0


@pytest.mark.parametrize(
    "df,expected",
    [
        (0.99, None),
        (1.0, 12.706),
        (3.7, 3.182),
        (30.0, 2.042),
        (30.999, 2.042),
        (31.0, 2.042),
        (39.9, 2.042),
        (40.0, 2.021),
        (120.0, 1.980),
        (100000.0, 1.960),
        (250000.0, 1.960),
    ],
)
def test_t_crit_truncates_and_floors_to_tabulated_df(df, expected):
    assert t_crit(df) == expected


def test_estimate_multi_fidelity_defaults_match_measurement_seconds():
    stats = estimate_multi_fidelity(
        n_candidates=4,
        n_confirm=2,
        measurement_seconds=30,
        screen_repetitions=3,
        confirm_repetitions=5,
        minimum_seconds=300,
    )
    assert stats["screen_seconds"] == 30.0
    assert stats["confirm_seconds"] == 30.0
    # Defaults reproduce the pre-existing formula: -60s, disabled.
    assert stats["estimated_saving_seconds"] == -60.0
    assert stats["enabled"] is False


def test_estimate_multi_fidelity_shorter_screen_increases_saving():
    kwargs = dict(
        n_candidates=4,
        n_confirm=1,
        measurement_seconds=30,
        screen_repetitions=3,
        confirm_repetitions=10,
        minimum_seconds=300,
    )
    default = estimate_multi_fidelity(**kwargs)
    shorter = estimate_multi_fidelity(**kwargs, screen_seconds=10, confirm_seconds=30)
    assert shorter["screen_seconds"] == 10.0
    assert shorter["confirm_seconds"] == 30.0
    assert shorter["estimated_saving_seconds"] > default["estimated_saving_seconds"]
    assert default["estimated_saving_seconds"] == 540.0
    assert shorter["estimated_saving_seconds"] == 780.0
    assert shorter["enabled"] is True


def test_estimate_multi_fidelity_enabled_boundary():
    base = dict(
        n_candidates=5,
        n_confirm=1,
        measurement_seconds=100,
        screen_repetitions=1,
        confirm_repetitions=1,
        screen_seconds=60,
        confirm_seconds=100,
    )
    # full=500, multi=5*60 + 1*100 = 400 -> saving=100s, pct=20.0.
    at_boundary = estimate_multi_fidelity(**base, minimum_seconds=100)
    assert at_boundary["estimated_saving_pct"] == 20.0
    assert at_boundary["estimated_saving_seconds"] == 100.0
    assert at_boundary["enabled"] is True

    # A minimum one second higher rejects the same run.
    assert estimate_multi_fidelity(**base, minimum_seconds=101)["enabled"] is False

    # 19% saving fails the percentage gate even with enough absolute seconds.
    below_pct = estimate_multi_fidelity(
        **{**base, "screen_seconds": 61}, minimum_seconds=0
    )
    assert below_pct["estimated_saving_pct"] < 20.0
    assert below_pct["enabled"] is False


def test_futility_stop_fires_only_for_clear_losers_after_min_reps():
    baseline = [100.0] * 10
    # Too few reps: never stop, even when behind.
    stop, stats = futility_stop(baseline, [80.0, 82.0, 81.0], min_reps=4)
    assert stop is False
    # Clear loser with enough reps: upper bound below zero.
    stop, stats = futility_stop(baseline, [80.0, 82.0, 81.0, 79.0], min_reps=4)
    assert stop is True
    assert stats["sufficient"] is True
    assert stats["ucb_pct"] < 0


def test_futility_stop_never_stops_winners_or_tossups():
    baseline = [100.0] * 10
    stop, _ = futility_stop(baseline, [105.0] * 10, min_reps=4)
    assert stop is False
    # Noisy but positive mean with an upper bound above zero: keep running.
    stop, stats = futility_stop(baseline, [106.0, 95.0, 110.0, 104.0], min_reps=4)
    assert stop is False
    assert stats["ucb_pct"] >= 0
