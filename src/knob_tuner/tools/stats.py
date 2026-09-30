"""Welch t screen/confirm statistics for the knob tuner (stdlib only).

Within-arm sysbench samples are sequential runs against the same mutating
database, so they are temporally correlated and Welch's independence
assumption does not hold. The confidence interval produced here is therefore a
**screen** — a way to rank and reject candidates — and never a guarantee. A
candidate may only be treated as a real improvement after an *independent*
confirmation measurement (fresh replicates against a reset database); callers
must not promote on this module's interval alone.

``lcb``/``ucb`` are expressed as a percentage of the baseline mean so
candidates measured at different baseline levels can be ranked together.
"""

from __future__ import annotations

import math
from statistics import mean, stdev
from typing import Any

# One-sided 0.025 t critical values (the confirmation test uses the same lookup).
# ponytail: floor of the Welch df is used (conservative for promotion) and the
# table is finite; swap in scipy.stats.t.ppf if scipy ever becomes a dependency.
_T_CRIT_025: dict[int, float] = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
    14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
    20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
    40: 2.021, 60: 2.000, 120: 1.980, 100000: 1.960,
}


def t_crit(df: float) -> float | None:
    """Return the one-sided 0.025 t critical value, or ``None`` if df < 1.

    ``df`` is truncated toward zero with ``int(df)`` and the lookup then floors
    to the nearest tabulated (smaller) df, so the returned critical value is
    never below the true one for the requested df. The table stops at 100000;
    any larger df uses that entry.
    """
    if df < 1:
        return None
    key = int(df)
    if key in _T_CRIT_025:
        return _T_CRIT_025[key]
    floor = max((k for k in _T_CRIT_025 if k <= key), default=1)
    return _T_CRIT_025[floor]


def welch_delta(
    baseline: list[float],
    tuned: list[float],
    alpha: float = 0.025,
) -> dict[str, Any]:
    """Compare two independent samples and return the delta with a confidence interval.

    The mapping always carries ``mean_delta_pct`` (tuned mean minus baseline
    mean, as a percentage of the baseline mean), ``lcb_pct``, ``ucb_pct``,
    ``se_pct``, ``df``, ``t_crit``, plus the diagnostic keys ``n1``, ``n2``,
    ``sufficient`` and ``zero_spread``. ``sufficient`` is ``False`` when an arm
    has fewer than two samples or the baseline mean is non-positive; the
    interval is then all zeros but ``df`` stays ``0.0``, so callers can tell
    "no evidence" apart from a genuine 0% delta. ``zero_spread`` marks a
    degenerate zero-variance pair, for which ``df`` is capped at the smallest
    tabulated value instead of claiming near-infinite evidence. ``df < 1``
    means the candidate is unrankable; callers must not default it.
    """
    n1, n2 = len(baseline), len(tuned)
    if n1 < 2 or n2 < 2:
        return {
            "mean_delta_pct": 0.0, "lcb_pct": 0.0, "ucb_pct": 0.0,
            "se_pct": 0.0, "df": 0.0, "t_crit": 0.0,
            "n1": n1, "n2": n2, "sufficient": False, "zero_spread": False,
        }

    m1, m2 = mean(baseline), mean(tuned)
    if m1 <= 0:
        return {
            "mean_delta_pct": 0.0, "lcb_pct": 0.0, "ucb_pct": 0.0,
            "se_pct": 0.0, "df": 0.0, "t_crit": 0.0,
            "n1": n1, "n2": n2, "sufficient": False, "zero_spread": False,
        }

    s1, s2 = stdev(baseline), stdev(tuned)
    var1, var2 = s1 * s1 / n1, s2 * s2 / n2
    se = math.sqrt(var1 + var2)
    se_pct = se / m1 * 100.0
    mean_delta_pct = (m2 - m1) / m1 * 100.0

    zero_spread = se == 0
    if zero_spread:
        # Both arms are constant, so the variance is unestimated rather than
        # zero: do not claim near-infinite evidence. A non-zero delta gets the
        # smallest tabulated df (widest t_crit); an identical pair gets df = 0.
        df = 1.0 if mean_delta_pct != 0 else 0.0
    else:
        df = (var1 + var2) ** 2 / (var1**2 / (n1 - 1) + var2**2 / (n2 - 1))

    tc = t_crit(df) or 0.0
    return {
        "mean_delta_pct": mean_delta_pct,
        "lcb_pct": mean_delta_pct - tc * se_pct,
        "ucb_pct": mean_delta_pct + tc * se_pct,
        "se_pct": se_pct,
        "df": df,
        "t_crit": tc,
        "n1": n1,
        "n2": n2,
        "sufficient": True,
        "zero_spread": zero_spread,
    }


def estimate_multi_fidelity(
    *,
    n_candidates: int,
    n_confirm: int,
    measurement_seconds: float,
    screen_repetitions: int,
    confirm_repetitions: int,
    minimum_seconds: float = 300.0,
    screen_seconds: float | None = None,
    confirm_seconds: float | None = None,
) -> dict[str, float | bool]:
    """Decide whether cheap screening + fresh confirmation actually saves time.

    ``full_seconds`` is every candidate measured at the full sample count;
    ``multi_seconds`` is every candidate screened cheaply plus a fresh
    confirmation for ``n_confirm`` of them. Per-arm prepare/warmup costs are
    assumed roughly equal in both arms, so they cancel. ``screen_seconds`` /
    ``confirm_seconds`` default to ``measurement_seconds`` so callers that do
    not shorten the screening run are unchanged.
    """
    screen_seconds = measurement_seconds if screen_seconds is None else screen_seconds
    confirm_seconds = (
        measurement_seconds if confirm_seconds is None else confirm_seconds
    )
    full_seconds = n_candidates * confirm_repetitions * confirm_seconds
    multi_seconds = (
        n_candidates * screen_repetitions * screen_seconds
        + n_confirm * confirm_repetitions * confirm_seconds
    )
    saving_seconds = full_seconds - multi_seconds
    saving_pct = (saving_seconds / full_seconds * 100.0) if full_seconds > 0 else 0.0
    enabled = saving_pct >= 20.0 and saving_seconds >= minimum_seconds
    return {
        "enabled": enabled,
        "estimated_saving_pct": round(saving_pct, 3),
        "estimated_saving_seconds": round(saving_seconds, 1),
        "configured_minimum_seconds": float(minimum_seconds),
        "screen_seconds": float(screen_seconds),
        "confirm_seconds": float(confirm_seconds),
    }


if __name__ == "__main__":  # ponytail: one runnable self-check, no framework
    flat = welch_delta([100.0, 100.0, 100.0], [105.0, 105.0, 105.0])
    assert flat["zero_spread"] is True and flat["df"] == 1.0
    assert flat["lcb_pct"] == flat["ucb_pct"] == 5.0
    none = welch_delta([100.0, 100.0, 100.0], [100.0, 100.0, 100.0])
    assert none["df"] == 0.0 and none["zero_spread"] is True and none["lcb_pct"] == 0.0
    thin = welch_delta([100.0], [105.0])
    assert thin["sufficient"] is False and thin["n1"] == 1 and thin["df"] == 0.0
    noisy = welch_delta([100.0, 110.0, 90.0], [130.0, 140.0, 120.0])
    assert noisy["sufficient"] is True and noisy["mean_delta_pct"] > 0
    assert noisy["lcb_pct"] < noisy["mean_delta_pct"] < noisy["ucb_pct"]
    assert t_crit(0.5) is None and t_crit(3.7) == 3.182
    assert t_crit(30.999) == 2.042 and t_crit(40.0) == 2.021
    narrow = estimate_multi_fidelity(
        n_candidates=4, n_confirm=2, measurement_seconds=30,
        screen_repetitions=3, confirm_repetitions=5, minimum_seconds=300,
    )
    assert narrow["estimated_saving_seconds"] == -60.0 and narrow["enabled"] is False
    wide = estimate_multi_fidelity(
        n_candidates=4, n_confirm=1, measurement_seconds=30,
        screen_repetitions=3, confirm_repetitions=10, minimum_seconds=300,
    )
    assert wide["estimated_saving_seconds"] == 540.0 and wide["enabled"] is True
    assert narrow["screen_seconds"] == narrow["confirm_seconds"] == 30.0
    shorter = estimate_multi_fidelity(
        n_candidates=4, n_confirm=1, measurement_seconds=30,
        screen_repetitions=3, confirm_repetitions=10, minimum_seconds=300,
        screen_seconds=10, confirm_seconds=30,
    )
    assert shorter["screen_seconds"] == 10.0 and shorter["confirm_seconds"] == 30.0
    assert shorter["estimated_saving_seconds"] == 780.0
    assert shorter["estimated_saving_seconds"] > wide["estimated_saving_seconds"]
    assert shorter["enabled"] is True
    print("stats self-check ok:", noisy, narrow, wide, shorter)
