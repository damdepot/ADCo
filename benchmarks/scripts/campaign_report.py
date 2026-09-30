#!/usr/bin/env python3
"""Aggregate per-round DCo campaign records into a per-scenario report.

Reads the JSON files written by ``benchmarks/run9_dco_campaign.sh`` and, for
each scenario, reports how often DCo promoted a knob and how much of the tuning
win transferred to the live production measurement.

Definitions (a round is "promoted" when DCo applied at least one knob):

  promotion rate  promoted rounds / total rounds
  TP  true positive   expected promotion, DCo promoted        (s1 hit)
  FN  false negative  expected promotion, DCo promoted nothing (s1 miss)
  FP  false positive  expected NO promotion, DCo promoted      (s2 miss)
  TN  true negative   expected NO promotion, DCo promoted none (s2 hit)
  production delta    mean/median per-round (dco - default)/default * 100, i.e.
                      the measured live speed-up of the DCo config over the
                      production default, from the interleaved A/B
  transfer gap        mean/median of (DCo gate LCB - production delta): how much
                      of the screening-gate confidence interval failed to show
                      up on the production measurement (only known when both the
                      gate LCB and the production delta exist)
  wall                mean/median DCo wall seconds per round

TP/FP/TN/FN are counted strictly against each scenario's ``ground_truth_expectation``.
For an expected-promotion scenario (s1) only TP/FN can be non-zero; for an
expected-no-promotion scenario (s2) only FP/TN can be non-zero.

Usage:
  .venv/bin/python benchmarks/scripts/campaign_report.py [--json OUT] results/campaign/*.json
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

LEGEND = {
    "promotion": "DCo applied >=1 knob in the round",
    "TP": "expected promotion and DCo promoted",
    "FN": "expected promotion and DCo promoted nothing",
    "FP": "expected no promotion but DCo promoted",
    "TN": "expected no promotion and DCo promoted nothing",
    "production_delta_pct": "(prod_dco_median_tps - prod_default_median_tps) / prod_default_median_tps * 100",
    "transfer_gap_pct": "dco_gate_lcb - production_delta_pct (None when either is unknown)",
    "promotion_rate": "promoted rounds / total rounds",
}


def expand(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        matches = [Path(p) for p in glob.glob(pattern)]
        if not matches and Path(pattern).is_file():
            matches = [Path(pattern)]
        for match in matches:
            if match.suffix == ".json" and match.is_file() and match not in paths:
                paths.append(match)
    return sorted(paths)


def _is_promoted(record: dict) -> bool:
    return len(record.get("dco_applied_knobs") or []) > 0


def _expected_promotion(record: dict) -> bool:
    return record.get("ground_truth_expectation") == "work_mem"


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def summarize(records: list[dict]) -> dict:
    promoted = sum(1 for r in records if _is_promoted(r))
    tp = fn = fp = tn = 0
    deltas: list[float] = []
    gaps: list[float] = []
    walls: list[float] = []
    for record in records:
        is_promoted = _is_promoted(record)
        if _expected_promotion(record):
            if is_promoted:
                tp += 1
            else:
                fn += 1
        else:
            if is_promoted:
                fp += 1
            else:
                tn += 1
        if record.get("prod_delta_pct") is not None:
            deltas.append(float(record["prod_delta_pct"]))
        if record.get("transfer_gap_pct") is not None:
            gaps.append(float(record["transfer_gap_pct"]))
        if record.get("dco_wall_seconds") is not None:
            walls.append(float(record["dco_wall_seconds"]))

    rounds = len(records)
    return {
        "rounds": rounds,
        "promotions": promoted,
        "promotion_rate": promoted / rounds if rounds else None,
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "tn": tn,
        "production_delta_pct": {
            "n": len(deltas),
            "mean": _mean(deltas),
            "median": _median(deltas),
        },
        "transfer_gap_pct": {
            "n": len(gaps),
            "mean": _mean(gaps),
            "median": _median(gaps),
        },
        "wall_seconds": {
            "n": len(walls),
            "mean": _mean(walls),
            "median": _median(walls),
        },
    }


def fmt(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}{suffix}"


def print_report(by_scenario: dict[str, list[dict]]) -> None:
    print("DCo campaign report")
    print("=" * 72)
    print("Counts (see legend at end): a round is promoted when DCo applied >=1 knob.")
    for scenario in sorted(by_scenario):
        summary = summarize(by_scenario[scenario])
        print()
        print(f"scenario: {scenario}")
        print(f"  rounds            = {summary['rounds']}")
        print(
            f"  promotions        = {summary['promotions']} "
            f"(promotion rate = {fmt(summary['promotion_rate'])})"
        )
        print(
            f"  TP/FP/TN/FN       = {summary['tp']}/{summary['fp']}/"
            f"{summary['tn']}/{summary['fn']}"
        )
        delta = summary["production_delta_pct"]
        gap = summary["transfer_gap_pct"]
        wall = summary["wall_seconds"]
        print(
            f"  production delta% = mean {fmt(delta['mean'], '%')} / "
            f"median {fmt(delta['median'], '%')} (n={delta['n']})"
        )
        print(
            f"  transfer gap%     = mean {fmt(gap['mean'], '%')} / "
            f"median {fmt(gap['median'], '%')} (n={gap['n']})"
        )
        print(
            f"  dco wall seconds  = mean {fmt(wall['mean'])} / "
            f"median {fmt(wall['median'])} (n={wall['n']})"
        )
    print()
    print("Legend")
    for key in ("promotion", "TP", "FN", "FP", "TN", "promotion_rate",
                "production_delta_pct", "transfer_gap_pct"):
        print(f"  {key:<20} {LEGEND[key]}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="Per-round JSON paths or globs")
    parser.add_argument("--json", help="Write the aggregate report to this path")
    args = parser.parse_args(argv)

    paths = expand(args.inputs)
    if not paths:
        print("No input JSON files matched.", file=sys.stderr)
        return 2

    by_scenario: dict[str, list[dict]] = {}
    skipped = 0
    for path in paths:
        try:
            record = json.loads(path.read_text())
        except Exception as exc:  # pragma: no cover - defensive
            print(f"WARNING: skipping unreadable {path}: {exc}", file=sys.stderr)
            skipped += 1
            continue
        if not isinstance(record, dict) or "scenario" not in record or "round" not in record:
            print(f"WARNING: skipping non-round JSON {path}", file=sys.stderr)
            skipped += 1
            continue
        if record.get("valid") is False:
            reason = record.get("invalid_reason") or "unspecified"
            print(
                f"WARNING: excluding invalid round {path.name}: {reason}",
                file=sys.stderr,
            )
            skipped += 1
            continue
        by_scenario.setdefault(record["scenario"], []).append(record)

    if not by_scenario:
        print("No valid campaign records found.", file=sys.stderr)
        return 2

    print_report(by_scenario)

    if args.json:
        aggregate = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "inputs": [str(p) for p in paths],
            "skipped": skipped,
            "legend": LEGEND,
            "scenarios": {
                scenario: summarize(records)
                for scenario, records in sorted(by_scenario.items())
            },
        }
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(aggregate, indent=2) + "\n")
        print(f"\nWrote aggregate JSON to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
