"""Unit tests for the knob_tuner Welch statistics helpers."""

import pytest

from src.knob_tuner.tools.stats import t_crit, welch_delta


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
