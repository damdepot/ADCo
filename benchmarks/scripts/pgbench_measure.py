#!/usr/bin/env python3
"""Run one pgbench measurement and write a noise_stats-compatible CSV.

Usage:
  .venv/bin/python benchmarks/scripts/pgbench_measure.py \
      --output results/pgbench/run.csv [--seconds 30] [--clients 2] ...

pgbench runs INSIDE the Postgres container (``docker exec -u postgres``)
because the macOS host<->container network path is a known client bottleneck
and would otherwise dominate the measurement. The custom script is copied into
the container with ``docker cp``.

work_mem is deliberately NOT touched here: the caller
(``benchmarks/run8_workmem_ab.sh``) owns the arm configuration so that this
tool stays a pure measurement primitive.

The CSV holds a single ``TOTAL,<tps>`` row so ``benchmarks/scripts/noise_stats.py``
can read it; the full pgbench output is written to ``<output>.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCRIPT = ROOT / "benchmarks" / "tools" / "pgbench" / "sort_hash.pgb"
DEFAULT_CONTAINER = "adcoexp-db"
DEFAULT_PGBENCH = "/usr/lib/postgresql/17/bin/pgbench"
REMOTE_SCRIPT = "/tmp/adco_pgbench_sort_hash.pgb"

TPS_RE = re.compile(r"^tps = ([0-9]+(?:\.[0-9]+)?)", re.MULTILINE)
LATENCY_RE = re.compile(r"^latency average = ([0-9]+(?:\.[0-9]+)?) ms", re.MULTILINE)
TXN_RE = re.compile(r"number of transactions actually processed: (\d+)")
FAILED_RE = re.compile(r"number of failed transactions: (\d+)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one in-container pgbench measurement and write a CSV/JSON record."
    )
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument("--db-name", default="adcodb", help="Database name (default adcodb)")
    parser.add_argument("--clients", type=int, default=2, help="pgbench clients (-c)")
    parser.add_argument("--threads", type=int, default=2, help="pgbench threads (-j)")
    parser.add_argument("--seconds", type=int, default=30, help="Measured duration (-T)")
    parser.add_argument(
        "--script",
        default=str(DEFAULT_SCRIPT),
        help="pgbench custom script (-f); default benchmarks/tools/pgbench/sort_hash.pgb",
    )
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help="Postgres container")
    parser.add_argument("--pgbench", default=DEFAULT_PGBENCH, help="pgbench path in container")
    parser.add_argument("--user", default="postgres", help="Database user")
    parser.add_argument("--label", default="", help="Optional label for logging")
    return parser


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.clients < 1 or args.threads < 1 or args.seconds < 1:
        print("--clients/--threads/--seconds must be >= 1", file=sys.stderr)
        return 2

    script = Path(args.script)
    if not script.is_file():
        print(f"pgbench script not found: {script}", file=sys.stderr)
        return 2

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    prefix = f"[{args.label}] " if args.label else ""

    copy = run(["docker", "cp", str(script), f"{args.container}:{REMOTE_SCRIPT}"])
    if copy.returncode != 0:
        print(f"{prefix}docker cp failed: {copy.stderr.strip()}", file=sys.stderr)
        return 1

    pgbench_cmd = [
        "docker",
        "exec",
        "-u",
        args.user,
        args.container,
        args.pgbench,
        "-n",
        "-f",
        REMOTE_SCRIPT,
        "-c",
        str(args.clients),
        "-j",
        str(args.threads),
        "-T",
        str(args.seconds),
        "-U",
        args.user,
        args.db_name,
    ]

    print(f"{prefix}pgbench -n -f {script.name} -c {args.clients} "
          f"-j {args.threads} -T {args.seconds}s db={args.db_name}", flush=True)
    started = time.perf_counter()
    proc = run(pgbench_cmd)
    wall = time.perf_counter() - started
    combined = (proc.stdout or "") + (proc.stderr or "")

    match = TPS_RE.search(combined)
    if proc.returncode != 0 or match is None:
        print(f"{prefix}pgbench failed (rc={proc.returncode})", file=sys.stderr)
        print(combined, file=sys.stderr)
        return 1

    tps = float(match.group(1))
    if tps <= 0:
        print(f"{prefix}pgbench produced non-positive TPS", file=sys.stderr)
        return 1

    latency = LATENCY_RE.search(combined)
    transactions = TXN_RE.search(combined)
    failed = FAILED_RE.search(combined)

    with open(output, "w", encoding="utf-8", newline="") as handle:
        handle.write(f"TOTAL,{tps:.6f}\n")

    record = {
        "output": str(output),
        "label": args.label,
        "db_name": args.db_name,
        "script": str(script),
        "container": args.container,
        "clients": args.clients,
        "threads": args.threads,
        "seconds": args.seconds,
        "tps": tps,
        "latency_avg_ms": float(latency.group(1)) if latency else None,
        "transactions": int(transactions.group(1)) if transactions else None,
        "failed_transactions": int(failed.group(1)) if failed else None,
        "wall_seconds": wall,
        "command": " ".join(shlex.quote(part) for part in pgbench_cmd),
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    with open(f"{output}.json", "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)

    print(f"{prefix}TOTAL (TPS) = {tps:.3f} (latency {record['latency_avg_ms']} ms, "
          f"txn {record['transactions']}) -> {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
