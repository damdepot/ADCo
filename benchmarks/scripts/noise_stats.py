#!/usr/bin/env python3
"""Summarize run-to-run noise from benchmark result CSVs.

Usage:
  noise_stats.py [--write-band PATH] [--protocol KEY=VALUE ...] <csv> [<csv> ...]

Each CSV must contain a ``TOTAL,<rate>`` row; the rate is the last column.

The band reports a descriptive spread plus a defensible statistic for deciding
whether a measured delta is real:

  stdev_pct = sd / mean * 100
      relative standard deviation of the run rates.
  cv_pct    = stdev_pct
      coefficient of variation (kept for backwards compatibility).
  ci95_pct  = t(0.975, n-1) * sd / sqrt(n) / mean * 100
      half-width of the 95% confidence interval for the mean rate.
  mde_pct   = (t(0.975, n-1) + t(0.80, n-1)) * sd * sqrt(2/n) / mean * 100
      minimum detectable difference between two independent means of n runs
      each, at 95% confidence and 80% power. This is the threshold a delta
      must exceed to be called PROVEN.

``spread_pct = (max - min) / median * 100`` is retained for backwards
compatibility with old band files and consumers, but it is an order statistic:
it grows with the sample count, is not comparable across different n, and must
not be used as a hard PROVEN/WITHIN-NOISE threshold. Prefer ``mde_pct`` (or
``ci95_pct``); ``tpcc_delta.py`` does exactly that and only falls back to
``spread_pct`` for legacy bands that lack the newer fields.

The t quantiles come from a small built-in table for df 1..30 and fall back to
the normal quantiles (1.96 for 0.975, 0.842 for 0.80) above that, because this
script is stdlib-only.

``--protocol KEY=VALUE`` records the measurement protocol (dataset, warmup,
runs) into the band's ``protocol`` object so the band cannot be read as
validating a different protocol.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from datetime import datetime, timezone

# One-sided Student-t quantiles for df 1..30 (df -> quantile). Above 30 we use
# the standard normal quantile, a close approximation for the sample sizes here.
_T975 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}
_T80 = {
    1: 1.376, 2: 1.061, 3: 0.978, 4: 0.941, 5: 0.920,
    6: 0.906, 7: 0.896, 8: 0.889, 9: 0.883, 10: 0.879,
    11: 0.876, 12: 0.873, 13: 0.870, 14: 0.868, 15: 0.866,
    16: 0.865, 17: 0.863, 18: 0.862, 19: 0.861, 20: 0.860,
    21: 0.859, 22: 0.858, 23: 0.858, 24: 0.857, 25: 0.856,
    26: 0.856, 27: 0.855, 28: 0.855, 29: 0.854, 30: 0.854,
}


def _t_crit(df: int, quantile: float) -> float:
    if quantile == 0.975:
        return _T975.get(df, 1.96)
    if quantile == 0.80:
        return _T80.get(df, 0.842)
    raise ValueError(f"unsupported t quantile {quantile}")


def total_rate(path: str) -> float:
    with open(path, newline="") as handle:
        for row in csv.reader(handle):
            if row and row[0].strip() == "TOTAL":
                return float(row[-1])
    raise ValueError(f"no TOTAL row in {path}")


def _usage() -> int:
    print(
        "usage: noise_stats.py [--write-band PATH] [--protocol KEY=VALUE ...] "
        "<csv> [<csv> ...]",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str]) -> int:
    band_path = None
    protocol: dict[str, str] = {}
    files: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--write-band":
            index += 1
            if index >= len(argv):
                return _usage()
            band_path = argv[index]
        elif arg == "--protocol":
            index += 1
            if index >= len(argv) or "=" not in argv[index]:
                return _usage()
            key, value = argv[index].split("=", 1)
            protocol[key.strip()] = value
        elif arg.startswith("--"):
            print(f"unknown option: {arg}", file=sys.stderr)
            return _usage()
        else:
            files.append(arg)
        index += 1

    if not files:
        return _usage()

    rates = [(path, total_rate(path)) for path in files]
    values = [rate for _, rate in rates]
    mean = statistics.mean(values)
    median = statistics.median(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    lo, hi = min(values), max(values)
    spread_pct = (hi - lo) / median * 100.0 if median else 0.0
    stdev_pct = sd / mean * 100.0 if mean else 0.0
    cv_pct = stdev_pct
    if len(values) > 1 and mean:
        df = len(values) - 1
        root_n = math.sqrt(len(values))
        ci95_pct = _t_crit(df, 0.975) * sd / root_n / mean * 100.0
        mde_pct = (
            (_t_crit(df, 0.975) + _t_crit(df, 0.80))
            * sd
            * math.sqrt(2.0 / len(values))
            / mean
            * 100.0
        )
    else:
        ci95_pct = 0.0
        mde_pct = 0.0

    print("Per-run TOTAL (txn/s):")
    for path, rate in rates:
        print(f"  {path.rsplit('/', 1)[-1]:<32} {rate:10.3f}")
    print()
    print(f"  n        = {len(values)}")
    print(f"  mean     = {mean:.3f}")
    print(f"  median   = {median:.3f}")
    print(f"  stdev    = {sd:.3f}  (stdev% = {stdev_pct:.2f}%, cv = {cv_pct:.2f}%)")
    print(f"  min/max  = {lo:.3f} / {hi:.3f}")
    print(f"  spread   = {spread_pct:.2f}% of median (order statistic, legacy)")
    print(f"  ci95     = +/-{ci95_pct:.2f}% of mean (95% CI half-width)")
    print(f"  mde      = {mde_pct:.2f}% of mean (95% conf / 80% power)")
    print()
    band = {
        "n": len(values),
        "mean": mean,
        "median": median,
        "stdev": sd,
        "stdev_pct": stdev_pct,
        "cv_pct": cv_pct,
        "ci95_pct": ci95_pct,
        "mde_pct": mde_pct,
        "min": lo,
        "max": hi,
        "spread_pct": spread_pct,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol": protocol,
    }
    if band_path:
        with open(band_path, "w") as handle:
            json.dump(band, handle, indent=2)
        print(f"  wrote noise band to {band_path}")
    print(
        "  a delta is only PROVEN when it exceeds mde_pct (ci95_pct is the "
        "single-mean CI; spread_pct is legacy and must not gate verdicts)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
