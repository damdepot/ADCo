#!/usr/bin/env python3
"""Run one sysbench measurement and write a noise_stats-compatible CSV.

Usage:
  .venv/bin/python benchmarks/scripts/sysbench_measure.py \
      --output results/sysbench/run.csv [--runs 1] [--seconds 30] ...

The CSV contains a single ``TOTAL,<median_tps>`` row so
``benchmarks/scripts/noise_stats.py`` can read it. The full measurement payload
is written to ``<output>.json`` for the record.

The measurement targets a dedicated database (default ``adcodb``) so the
sysbench workload never pollutes the TPC-C database; override with the
``SYSBENCH_DB`` environment variable or ``--db-name``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.knob_tuner.contracts import SysbenchProfile  # noqa: E402
from src.knob_tuner.tools.benchmark_tools import (  # noqa: E402
    run_sysbench_measurement,
)
from src.knob_tuner.tools.db_connector import load_db_config  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one sysbench measurement and write a CSV/JSON record."
    )
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of separate measurement invocations to aggregate (default 1)",
    )
    parser.add_argument("--db-type", default="postgres", help="Database type")
    parser.add_argument(
        "--db-name",
        default=os.environ.get("SYSBENCH_DB", "adcodb"),
        help="Database name (default: $SYSBENCH_DB or adcodb)",
    )
    parser.add_argument(
        "--config",
        default=str(ROOT / "db.config"),
        help="Path to the INI database config (default: repo-root db.config)",
    )
    parser.add_argument("--seconds", type=int, default=30, help="Measured seconds")
    parser.add_argument("--threads", type=int, default=4, help="Worker threads")
    parser.add_argument(
        "--repetitions",
        type=int,
        default=3,
        help="Measured repetitions *within* one invocation (default 3)",
    )
    parser.add_argument("--tables", type=int, default=10, help="Number of tables")
    parser.add_argument(
        "--table-size", type=int, default=10000, help="Rows per table"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--prepare",
        dest="prepare",
        action="store_true",
        default=None,
        help="Prepare the dataset on the first invocation (default)",
    )
    group.add_argument(
        "--no-prepare",
        dest="prepare",
        action="store_false",
        help="Reuse an existing dataset; never prepare",
    )
    parser.add_argument("--label", default="", help="Optional label for logging")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.runs < 1:
        print("--runs must be >= 1", file=sys.stderr)
        return 2

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    cfg = load_db_config(args.config, db_type=args.db_type, db_override=args.db_name)
    profile = SysbenchProfile(
        tables=args.tables,
        rows_per_table=args.table_size,
        threads=args.threads,
        measurement_seconds=args.seconds,
        repetitions=args.repetitions,
    )

    prefix = f"[{args.label}] " if args.label else ""
    progress = lambda message: print(f"{prefix}{message}", flush=True)  # noqa: E731

    payloads = []
    pooled_tps: list[float] = []
    for index in range(args.runs):
        # Prepare on EVERY invocation when --prepare is requested. sysbench
        # oltp_read_write mutates its dataset (inserts/deletes), so reusing a
        # prepared dataset makes successive reps slower and adds large
        # run-to-run spread. A fresh dataset per sample is the stable unit.
        do_prepare = args.prepare is not False
        print(
            f"{prefix}invocation {index + 1}/{args.runs} "
            f"(db={cfg.database}, prepare={do_prepare})",
            flush=True,
        )
        measurement = run_sysbench_measurement(
            cfg, profile, prepare=do_prepare, progress=progress
        )
        if measurement.status != "ok":
            print(
                f"sysbench measurement failed: {measurement.error}",
                file=sys.stderr,
            )
            return 1
        payloads.append(measurement.model_dump())
        pooled_tps.extend(measurement.per_run_tps)
        if not measurement.per_run_tps:
            pooled_tps.append(measurement.tps)

    total_tps = float(median(pooled_tps)) if pooled_tps else 0.0
    if total_tps <= 0:
        print("sysbench produced no positive TPS samples", file=sys.stderr)
        return 1

    with open(output, "w", encoding="utf-8", newline="") as handle:
        handle.write(f"TOTAL,{total_tps:.6f}\n")

    record = {
        "output": str(output),
        "db_name": cfg.database,
        "db_type": cfg.db_type,
        "label": args.label,
        "runs": args.runs,
        "pooled_per_run_tps": pooled_tps,
        "total_tps": total_tps,
        "measurements": payloads,
    }
    with open(f"{output}.json", "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)

    print(f"{prefix}TOTAL (median TPS) = {total_tps:.3f} -> {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
