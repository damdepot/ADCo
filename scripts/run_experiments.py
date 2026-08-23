"""Run multiple ADCo pipeline experiments (rewrite → check → benchmark).

Usage:
    uv run python scripts/run_experiments.py --type tpcc --runs 10
    uv run python scripts/run_experiments.py --type smallbank --runs 10
    uv run python scripts/run_experiments.py --type both --runs 10 --delay 30

Results are printed to stdout as a summary table and saved as CSV under
scripts/results/experiments_<type>_<timestamp>.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "scripts" / "results"

BENCHMARK_DIRS: dict[str, Path] = {
    "tpcc": ROOT / "benchmarks" / "benchmark_tpcc",
    "smallbank": ROOT / "benchmarks" / "benchmark_smallbank",
}

TPCC_ARGS = [
    "mysql",
    "--clients=1",
    "--warehouses=1",
    "--duration=60",
    "--reset",
]

TPCC_CONFIG_PATH = "configs/mysql.config"

SMALLBANK_ARGS = [
    "test",
    "--accounts", "3000",
    "--transactions", "15000",
    "--threads", "1",
]


def main() -> None:
    args = parse_args()
    types_to_run = ["tpcc", "smallbank"] if args.benchmark_type == "both" else [args.benchmark_type]

    for bench_type in types_to_run:
        print(f"\n{'#' * 60}")
        print(f"# {args.runs}x {bench_type.upper()} EXPERIMENTS")
        print(f"# Model: {args.model}  Delay: {args.delay}s")
        print(f"{'#' * 60}")

        results: list[dict[str, Any]] = []
        for run_num in range(1, args.runs + 1):
            print(f"\n>>> Experiment {run_num}/{args.runs} ({bench_type})")
            result = run_pipeline(bench_type, model=args.model)
            results.append(result)
            print(f"\n>>> Run {run_num} completed in {result['pipeline_duration']:.1f}s")

            if run_num < args.runs and args.delay > 0:
                print(f"Sleeping {args.delay}s ...")
                time.sleep(args.delay)

        print_summary(results)
        save_csv(results, bench_type)

        if len(types_to_run) > 1 and bench_type != types_to_run[-1] and args.delay > 0:
            print(f"\nSleeping {args.delay}s before switching benchmark type ...")
            time.sleep(args.delay)


# ─── argument parsing ────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run multiple ADCo experiments (rewrite → check → benchmark)",
    )
    parser.add_argument(
        "--type", dest="benchmark_type", required=True,
        choices=["tpcc", "smallbank", "both"],
        help="Benchmark type to run",
    )
    parser.add_argument(
        "--runs", type=int, default=10,
        help="Number of experiments (default: 10)",
    )
    parser.add_argument(
        "--delay", type=int, default=30,
        help="Cooldown seconds between pipelines (default: 30)",
    )
    parser.add_argument(
        "--model", default="gemini-3.5-flash-lite",
        help="LLM model for rewriter and checker (default: gemini-3.5-flash-lite)",
    )
    return parser.parse_args()


# ─── command helpers ─────────────────────────────────────────────────────────

def run_command(cmd: list[str], timeout: int = 900) -> tuple[int, str, str, float]:
    """Run *cmd* and return (exit_code, stdout, stderr, duration_seconds)."""
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=str(ROOT), timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"TIMEOUT after {timeout}s", time.time() - started
    return proc.returncode, proc.stdout or "", proc.stderr or "", time.time() - started


def parse_sandbox(stdout: str) -> str | None:
    """Extract sandbox path from rewriter output line: ``Sandbox:   /path/...``"""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Sandbox:"):
            return stripped.split(":", 1)[1].strip()
    return None


def parse_rewrite_status(stdout: str) -> str:
    """Extract PASS/FAIL from rewriter output."""
    for line in stdout.splitlines():
        if "Pipeline PASSED" in line:
            return "PASS"
        if "Pipeline FAILED" in line:
            return "FAIL"
    return "UNKNOWN"


def parse_check_result(stdout: str) -> str:
    """Extract Result value from checker output line: ``Result:   PASS``"""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Result:"):
            return stripped.split(":", 1)[1].strip()
    return "UNKNOWN"


def parse_benchmark_tps(stdout: str) -> float:
    """Extract TPS from benchmark runner output.

    Prefers the ``Logged: ... txn/s`` summary line from the benchmark runner,
    then falls back to the last ``txn/s`` occurrence in raw benchmark output.
    """
    for line in reversed(stdout.splitlines()):
        if line.startswith("Logged:"):
            m = re.search(r"([\d.]+)\s*txn/s", line)
            if m:
                return float(m.group(1))

    for line in reversed(stdout.splitlines()):
        m = re.search(r"([\d.]+)\s*txn/s", line)
        if m:
            return float(m.group(1))
    return 0.0


# ─── pipeline runner ─────────────────────────────────────────────────────────

def run_pipeline(bench_type: str, model: str) -> dict[str, Any]:
    """Execute one full pipeline and return a result dict."""
    result: dict[str, Any] = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sandbox": "-",
        "rewrite_status": "FAIL",
        "rewrite_duration": 0.0,
        "check_status": "-",
        "check_duration": 0.0,
        "benchmark_tps": 0.0,
        "benchmark_duration": 0.0,
        "pipeline_duration": 0.0,
    }
    pipeline_start = time.time()
    source_dir = BENCHMARK_DIRS[bench_type]

    # ── step 1: rewrite ──
    print(f"\n{'─' * 60}")
    print(f"[REWRITE] {bench_type} from {source_dir}")
    rc, stdout, stderr, dur = run_command([
        "uv", "run", "python", "-m", "src.rewriter", str(source_dir),
        "--model", model,
    ])
    result["rewrite_duration"] = dur
    result["rewrite_status"] = parse_rewrite_status(stdout)
    _print_tail(stdout, stderr, 600)

    if rc != 0 or result["rewrite_status"] != "PASS":
        result["pipeline_duration"] = time.time() - pipeline_start
        print("  ❌ REWRITE FAILED — skipping")
        return result

    sandbox = parse_sandbox(stdout)
    if not sandbox:
        result["pipeline_duration"] = time.time() - pipeline_start
        print("  ❌ Could not parse sandbox path — skipping")
        return result

    result["sandbox"] = os.path.basename(sandbox)

    # ── step 2: check ──
    print(f"\n[CHECK] {sandbox}")
    rc, stdout, stderr, dur = run_command([
        "uv", "run", "python", "-m", "src.checker", sandbox,
        "--model", model,
    ])
    result["check_duration"] = dur
    result["check_status"] = parse_check_result(stdout)
    _print_tail(stdout, stderr, 600)

    if result["check_status"] == "FAIL":
        print("  ⚠ CHECK FAILED — continuing to benchmark anyway")

    # ── step 3: benchmark ──
    print(f"\n[BENCHMARK] {bench_type} from {sandbox}")

    bench_base_cmd = [
        "uv", "run", "python", "-m", "benchmarks.src",
        sandbox, "--type", bench_type,
    ]
    if bench_type == "tpcc":
        bench_cmd = bench_base_cmd + TPCC_ARGS + ["--config", os.path.join(sandbox, TPCC_CONFIG_PATH)]
    else:
        bench_cmd = bench_base_cmd + SMALLBANK_ARGS

    rc, stdout, stderr, dur = run_command(bench_cmd, timeout=900)
    result["benchmark_duration"] = dur
    result["benchmark_tps"] = parse_benchmark_tps(stdout)
    _print_tail(stdout, stderr, limit=30, err_limit=8)

    if rc != 0:
        print(f"  ❌ BENCHMARK FAILED (exit {rc})")

    result["pipeline_duration"] = time.time() - pipeline_start
    return result


def _print_tail(stdout: str, stderr: str, limit: int, err_limit: int | None = None) -> None:
    """Print last *limit* lines of stdout and *err_limit* lines of stderr.

    If *err_limit* is None, it defaults to *limit*.
    """
    if err_limit is None:
        err_limit = limit
    if stdout:
        lines = stdout.splitlines()
        tail = lines[-limit:] if len(lines) > limit else lines
        print("\n".join(tail))
    if stderr:
        lines = stderr.splitlines()
        tail = lines[-err_limit:] if len(lines) > err_limit else lines
        print("\n".join(tail), file=sys.stderr)


# ─── reporting ───────────────────────────────────────────────────────────────

def print_summary(results: list[dict[str, Any]]) -> None:
    print(f"\n{'=' * 85}")
    print("EXPERIMENT SUMMARY")
    print(f"{'=' * 85}")

    header = (
        f"{'Run':<5} {'Started':<20} {'Sandbox':<14} "
        f"{'Rewrite':<8} {'Check':<7} {'TPS':<10} {'Dur(s)':<8}"
    )
    print(header)
    print("-" * len(header))

    for i, r in enumerate(results, 1):
        print(
            f"{i:<5} {r['timestamp']:<20} {r['sandbox']:<14} "
            f"{r['rewrite_status']:<8} {r['check_status']:<7} "
            f"{r['benchmark_tps']:<10.1f} {r['pipeline_duration']:<8.1f}"
        )

    print("-" * len(header))

    passed = sum(1 for r in results if r["rewrite_status"] == "PASS")
    tps_vals = [r["benchmark_tps"] for r in results if r["benchmark_tps"] > 0]

    print(f"Passed: {passed}/{len(results)}")
    if tps_vals:
        print(f"Avg TPS: {sum(tps_vals) / len(tps_vals):.1f}")

    total_dur = sum(r["pipeline_duration"] for r in results)
    print(f"Total duration: {total_dur:.0f}s ({total_dur / 60:.1f}m)")


def save_csv(results: list[dict[str, Any]], benchmark_type: str) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"experiments_{benchmark_type}_{ts}.csv"

    fieldnames = [
        "run", "timestamp", "sandbox", "rewrite_status", "rewrite_duration",
        "check_status", "check_duration", "benchmark_tps", "benchmark_duration",
        "pipeline_duration",
    ]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, r in enumerate(results, 1):
            writer.writerow({"run": i, **r})

    print(f"\nResults saved to: {path}")


if __name__ == "__main__":
    main()
