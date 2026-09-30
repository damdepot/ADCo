#!/usr/bin/env python3
"""Compare benchmark result CSVs and judge deltas against noise bands.

Legacy (single workload, backwards compatible):
    tpcc_delta.py <baseline.csv> <optimized.csv> [noise_band.json]

Dual workload (baseline vs one or more tuned arms, TPC-C + sysbench):
    tpcc_delta.py dual \
        --baseline-tpcc <csv> [<csv> ...] \
        --baseline-sysbench <csv> [<csv> ...] \
        --arm <name>:<tpcc,csv,...>:<sysbench,csv,...> [--arm ...] \
        [--band-tpcc <json>] [--band-sysbench <json>] \
        [--output <json>] [--run-id <id>] [--reps <n>]

Each ``--arm`` spec is ``name:tpcc_csvs:sysbench_csvs`` where the CSV lists are
comma-separated. An arm with empty lists (``strict::``) is reported as
``NOT APPLIED`` and excluded from the verdict.

A delta is PROVEN only when it exceeds the band's defensible noise threshold,
chosen in this order:

1. ``mde_pct``  - minimum detectable effect (two-sample, 95% conf / 80% power)
2. ``ci95_pct`` - 95% CI half-width of the mean
3. ``spread_pct`` - legacy max-min order statistic, used ONLY for band files
   that predate ``mde_pct``/``ci95_pct``.

The rule and the exact numbers compared (delta %, threshold %, basis) are
printed so no verdict is a black box.

It also implements the G1 guard: a tuned arm whose sysbench delta is a WIN but
whose TPC-C delta is a proven REGRESSION is reported as ``DEGRADED``, never as
a win.

Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import datetime, timezone

WIN = "WIN"
REGRESSION = "REGRESSION"
NO_EFFECT = "NO EFFECT"
UNKNOWN = "UNKNOWN (no band)"
DEGRADED = "DEGRADED"
REGRESSED = "REGRESSED"
NOT_APPLIED = "NOT APPLIED"


def read_rates(path: str) -> dict[str, float]:
    rates: dict[str, float] = {}
    with open(path, newline="") as handle:
        for row in csv.reader(handle):
            if row and row[0].strip() and len(row) >= 2:
                try:
                    rates[row[0].strip()] = float(row[-1])
                except ValueError:
                    continue
    return rates


def total_rate(path: str) -> float:
    return read_rates(path).get("TOTAL", 0.0)


def load_band(path: str | None) -> dict | None:
    if not path:
        return None
    try:
        with open(path) as handle:
            return json.load(handle)
    except OSError:
        return None


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _pct_change(baseline: float, value: float) -> float:
    return (value - baseline) / baseline * 100.0 if baseline else 0.0


def band_threshold(band: dict | None) -> tuple[float, str]:
    """Return (threshold_pct, basis) using the most defensible field present.

    ``mde_pct`` is the two-sample minimum detectable effect; ``ci95_pct`` is the
    single-mean 95% CI half-width; ``spread_pct`` is the legacy max-min order
    statistic and is only used when the newer fields are absent.
    """
    if band is None:
        return 0.0, "none"
    for key in ("mde_pct", "ci95_pct", "spread_pct"):
        if key in band:
            try:
                return float(band[key]), key
            except (TypeError, ValueError):
                continue
    return 0.0, "none"


def classify(delta_pct: float, band: dict | None) -> tuple[str, float, str]:
    """Classify a delta and return (verdict, threshold_pct, basis)."""
    if band is None:
        return UNKNOWN, 0.0, "none"
    threshold, basis = band_threshold(band)
    if basis == "none":
        return UNKNOWN, threshold, basis
    if abs(delta_pct) > threshold:
        return (WIN if delta_pct > 0 else REGRESSION), threshold, basis
    return NO_EFFECT, threshold, basis


def workload_verdict(delta_pct: float, band: dict | None) -> str:
    """Classify a single workload delta against its noise band."""
    return classify(delta_pct, band)[0]


def band_summary(band: dict | None) -> str:
    """One-line human summary of the threshold a band will gate verdicts with."""
    if band is None:
        return "MISSING"
    threshold, basis = band_threshold(band)
    if basis == "none":
        return "NO THRESHOLD FIELD"
    spread = band.get("spread_pct")
    extra = ""
    if spread is not None and basis != "spread_pct":
        try:
            extra = f", spread={float(spread):.2f}%"
        except (TypeError, ValueError):
            extra = ""
    return f"threshold={threshold:.2f}% ({basis}{extra})"


def overall_verdict(tpcc: str, sysbench: str) -> str:
    """Combine the two workload verdicts, applying the G1 non-regression guard."""
    if tpcc == REGRESSION or sysbench == REGRESSION:
        # G1: sysbench win + proven TPC-C regression is a distinct failure.
        if sysbench == WIN and tpcc == REGRESSION:
            return DEGRADED
        return REGRESSED
    if tpcc == WIN and sysbench == WIN:
        return WIN
    if {tpcc, sysbench} <= {WIN, NO_EFFECT} and WIN in (tpcc, sysbench):
        return WIN
    if tpcc == NO_EFFECT and sysbench == NO_EFFECT:
        return NO_EFFECT
    return UNKNOWN


def run_legacy(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: tpcc_delta.py <baseline.csv> <optimized.csv> [noise.json]",
            file=sys.stderr,
        )
        return 2
    baseline = read_rates(argv[0])
    optimized = read_rates(argv[1])
    band = load_band(argv[2] if len(argv) > 2 else None)

    print(f"baseline : {argv[0]}")
    print(f"optimized: {argv[1]}")
    print(f"{'transaction':<14}{'baseline':>12}{'optimized':>12}{'delta%':>10}")
    total_delta = 0.0
    for name in sorted(set(baseline) | set(optimized)):
        if name == "TOTAL":
            continue
        b = baseline.get(name, 0.0)
        o = optimized.get(name, 0.0)
        delta = (o - b) / b * 100.0 if b else 0.0
        print(f"{name:<14}{b:>12.3f}{o:>12.3f}{delta:>9.2f}%")
    b_total = baseline.get("TOTAL", 0.0)
    o_total = optimized.get("TOTAL", 0.0)
    if b_total:
        total_delta = (o_total - b_total) / b_total * 100.0
    print(f"{'TOTAL':<14}{b_total:>12.3f}{o_total:>12.3f}{total_delta:>9.2f}%")

    if band is None:
        print()
        print("noise band: MISSING -> TOTAL verdict UNKNOWN (no band)")
    else:
        verdict, threshold, basis = classify(total_delta, band)
        if basis == "none":
            print()
            print("noise band: no usable threshold field -> verdict UNKNOWN")
        else:
            proven = "PROVEN" if verdict in (WIN, REGRESSION) else "WITHIN NOISE (unproven)"
            print()
            print(
                f"noise band threshold = {threshold:.2f}% (basis: {basis}); "
                f"TOTAL delta {total_delta:+.2f}% is {proven}"
            )
    return 0


def _parse_arm_spec(spec: str) -> tuple[str, list[str], list[str]]:
    parts = spec.split(":", 2)
    if len(parts) != 3:
        raise SystemExit(
            f"invalid --arm spec {spec!r}; expected name:tpcc_csvs:sysbench_csvs"
        )
    name, tpcc_part, sys_part = parts
    tpcc = [p for p in (tpcc_part.split(",") if tpcc_part else []) if p]
    sysbench = [p for p in (sys_part.split(",") if sys_part else []) if p]
    return name.strip(), tpcc, sysbench


def _arm_report(
    tpcc_csvs: list[str],
    sys_csvs: list[str],
    baseline_tpcc: float,
    baseline_sys: float,
    band_tpcc: dict | None,
    band_sys: dict | None,
) -> dict:
    if not tpcc_csvs or not sys_csvs:
        return {"applied": False, "verdict": NOT_APPLIED}

    tpcc_vals = [total_rate(path) for path in tpcc_csvs]
    sys_vals = [total_rate(path) for path in sys_csvs]
    tpcc_med = _median(tpcc_vals)
    sys_med = _median(sys_vals)
    tpcc_delta = _pct_change(baseline_tpcc, tpcc_med)
    sys_delta = _pct_change(baseline_sys, sys_med)
    tpcc_v, tpcc_thr, tpcc_basis = classify(tpcc_delta, band_tpcc)
    sys_v, sys_thr, sys_basis = classify(sys_delta, band_sys)
    return {
        "applied": True,
        "tpcc": {
            "median_tps": tpcc_med,
            "delta_pct": tpcc_delta,
            "threshold_pct": tpcc_thr,
            "threshold_basis": tpcc_basis,
            "verdict": tpcc_v,
            "csvs": list(tpcc_csvs),
        },
        "sysbench": {
            "median_tps": sys_med,
            "delta_pct": sys_delta,
            "threshold_pct": sys_thr,
            "threshold_basis": sys_basis,
            "verdict": sys_v,
            "csvs": list(sys_csvs),
        },
        "verdict": overall_verdict(tpcc_v, sys_v),
    }


def run_dual(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tpcc_delta.py dual")
    parser.add_argument("--baseline-tpcc", nargs="+", required=True)
    parser.add_argument("--baseline-sysbench", nargs="+", required=True)
    parser.add_argument("--arm", action="append", default=[], dest="arms")
    parser.add_argument("--band-tpcc", default=None)
    parser.add_argument("--band-sysbench", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--reps", type=int, default=0)
    args = parser.parse_args(argv)

    band_tpcc = load_band(args.band_tpcc)
    band_sys = load_band(args.band_sysbench)
    if band_tpcc is None:
        print(
            f"WARNING: no TPC-C noise band at {args.band_tpcc!r}; "
            "TPC-C verdicts will be UNKNOWN (no band).",
            file=sys.stderr,
        )
    if band_sys is None:
        print(
            f"WARNING: no sysbench noise band at {args.band_sysbench!r}; "
            "sysbench verdicts will be UNKNOWN (no band).",
            file=sys.stderr,
        )

    baseline_tpcc_vals = [total_rate(path) for path in args.baseline_tpcc]
    baseline_sys_vals = [total_rate(path) for path in args.baseline_sysbench]
    baseline_tpcc = _median(baseline_tpcc_vals)
    baseline_sys = _median(baseline_sys_vals)

    arms: dict[str, dict] = {}
    for spec in args.arms:
        name, tpcc_csvs, sys_csvs = _parse_arm_spec(spec)
        arms[name] = _arm_report(
            tpcc_csvs, sys_csvs, baseline_tpcc, baseline_sys, band_tpcc, band_sys
        )

    report = {
        "run_id": args.run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reps": args.reps,
        "bands": {"tpcc": band_tpcc, "sysbench": band_sys},
        "baseline": {
            "tpcc_median_tps": baseline_tpcc,
            "sysbench_median_tps": baseline_sys,
            "tpcc_csvs": list(args.baseline_tpcc),
            "sysbench_csvs": list(args.baseline_sysbench),
        },
        "arms": arms,
    }

    print(f"=== Dual-workload arm report ({args.run_id or 'no run id'}) ===")
    print(f"TPC-C    band: {band_summary(band_tpcc)}  ({args.band_tpcc})")
    print(f"sysbench band: {band_summary(band_sys)}  ({args.band_sysbench})")
    print(f"reps = {args.reps}  baseline TPC-C median = {baseline_tpcc:.3f}"
          f"  baseline sysbench median = {baseline_sys:.3f}")
    print()
    header = f"{'arm':<10}{'workload':<11}{'median':>12}{'delta%':>10}{'thr%':>9}  {'verdict':<20}{'overall':<11}"
    print(header)
    print("-" * len(header))
    for name, arm in arms.items():
        if not arm.get("applied"):
            print(f"{name:<10}{'--':<11}{'--':>12}{'--':>10}{'--':>9}  {NOT_APPLIED:<20}{NOT_APPLIED:<11}")
            continue
        for workload, label in (("tpcc", "TPC-C"), ("sysbench", "sysbench")):
            data = arm[workload]
            basis = data.get("threshold_basis", "none")
            print(
                f"{name:<10}{label:<11}{data['median_tps']:>12.3f}"
                f"{data['delta_pct']:>9.2f}%{data.get('threshold_pct', 0.0):>8.2f}%"
                f"  {data['verdict']:<20}{arm['verdict']:<11}"
            )
            print(f"           threshold basis: {basis}")
        if arm["verdict"] == DEGRADED:
            print(
                f"  G1: sysbench {arm['sysbench']['verdict']} but TPC-C "
                f"{arm['tpcc']['verdict']} -> {DEGRADED}"
            )

    print()
    print("Overall arm verdicts:")
    for name, arm in arms.items():
        print(f"  {name:<10} {arm['verdict']}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, default=str)
        print()
        print(f"wrote machine-readable report to {args.output}")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "dual":
        return run_dual(argv[1:])
    return run_legacy(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
