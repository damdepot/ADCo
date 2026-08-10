"""Benchmark runner — subprocess wrapper with telemetry logging.

Runs TPC-C or SmallBank from a sandbox directory, captures output, and logs
metrics and console output. Does NOT modify benchmark source code.

Usage:
    uv run python -m benchmark_runner <sandbox> --type tpcc [args...]
    uv run python -m benchmark_runner <sandbox> --type smallbank [args...]
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
import time

from telemetry import RewriterRun

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(ROOT, "logs")

_TOTAL_RE = re.compile(
    r"TOTAL\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+txn/s"
)


def _log_console(benchmark: str, text: str) -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"{benchmark}.log")
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a") as f:
        f.write(f"\n{'=' * 60}\n")
        f.write(f"  {ts}\n")
        f.write(f"{'=' * 60}\n")
        f.write(text)
        f.write("\n")


def _parse_total(stdout: str) -> tuple[int, float, float]:
    match = _TOTAL_RE.search(stdout)
    if not match:
        return 0, 0.0, 0.0
    return int(match.group(1)), float(match.group(2)), float(match.group(3))


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark runner with telemetry logging",
    )
    parser.add_argument(
        "sandbox",
        help="Path to sandbox directory (e.g. sandbox/abc123)",
    )
    parser.add_argument(
        "--type", dest="benchmark", required=True,
        choices=["tpcc", "smallbank"],
        help="Benchmark type",
    )
    parser.add_argument(
        "--driver", default="mysql",
        help="Database driver name",
    )
    parser.add_argument(
        "--timeout", type=int, default=900,
        help="Max seconds before killing",
    )

    args, remaining = parser.parse_known_args()

    sandbox = os.path.abspath(args.sandbox)
    if not os.path.isdir(sandbox):
        print(f"ERROR: not a directory: {sandbox}", file=sys.stderr)
        sys.exit(2)

    sandbox_id = os.path.basename(sandbox)
    benchmark = args.benchmark

    if benchmark == "tpcc":
        entry = os.path.join(sandbox, "tpcc.py")
    else:
        entry = os.path.join(sandbox, "main.py")

    if not os.path.isfile(entry):
        print(f"ERROR: entry point not found: {entry}", file=sys.stderr)
        sys.exit(2)

    cmd = [sys.executable, entry] + remaining

    print(f"\n=== {benchmark.upper()} ===")
    print(f"Sandbox:  {sandbox}")
    print(f"Command:  {' '.join(cmd)}")

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": sandbox}
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=ROOT, env=env, timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - started) * 1000)
        console = (
            f"Command:  {' '.join(cmd)}\n"
            f"Exit:     TIMEOUT\n"
            f"Duration: {duration_ms}ms (timeout={args.timeout}s)\n"
        )
        _log_console(benchmark, console)
        print(f"TIMEOUT after {args.timeout}s")
        with RewriterRun(run_type="benchmark", sandbox_id=sandbox_id) as telemetry:
            telemetry.record_benchmark(
                benchmark=benchmark, driver=args.driver,
                duration_ms=duration_ms, exit_code=-1,
            )
        sys.exit(1)

    duration_ms = int((time.time() - started) * 1000)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    console = (
        f"Command:  {' '.join(cmd)}\n"
        f"Exit:     {proc.returncode}\n"
        f"Duration: {duration_ms}ms\n"
        f"{'-' * 40}\n"
        f"{stdout.strip()}\n"
    )
    if stderr.strip():
        console += f"\nSTDERR:\n{stderr.strip()}\n"

    _log_console(benchmark, console)

    if proc.returncode != 0:
        print(f"\n=== FAILED (exit {proc.returncode}) ===")
        if stderr:
            print(stderr.strip()[-2000:])
        with RewriterRun(run_type="benchmark", sandbox_id=sandbox_id) as telemetry:
            telemetry.record_benchmark(
                benchmark=benchmark, driver=args.driver,
                duration_ms=duration_ms, exit_code=proc.returncode,
            )
        sys.exit(proc.returncode)

    total_executed, total_time_us, total_tps = _parse_total(stdout)
    print(stdout.strip())

    with RewriterRun(run_type="benchmark", sandbox_id=sandbox_id) as telemetry:
        telemetry.record_benchmark(
            benchmark=benchmark, driver=args.driver,
            duration_ms=duration_ms, exit_code=0,
            total_executed=total_executed, total_time_us=total_time_us,
            total_tps=total_tps,
        )

    print(f"\nLogged: {total_executed} txns, {total_tps:.2f} txn/s, {duration_ms}ms")


if __name__ == "__main__":
    main()
