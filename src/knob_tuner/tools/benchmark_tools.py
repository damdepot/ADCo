"""Sysbench benchmark runner tool for knob_tuner."""

import os
import re
import subprocess
import time
from typing import Any

from .db_connector import DBConfig, get_connection


def _parse_sysbench_summary(output: str) -> dict[str, Any]:
    """Parse sysbench run output summary section for query counts and latency statistics."""
    details: dict[str, Any] = {}

    # Queries
    queries_match = re.search(r"queries:\s+(\d+)", output)
    if queries_match:
        details["total_queries"] = int(queries_match.group(1))
    else:
        total_match = re.search(r"total:\s+(\d+)", output)
        if total_match:
            details["total_queries"] = int(total_match.group(1))

    # Transactions
    txn_match = re.search(r"transactions:\s+(\d+)", output)
    if txn_match:
        details["total_transactions"] = int(txn_match.group(1))

    # Read / write / other queries
    read_match = re.search(r"read:\s+(\d+)", output)
    if read_match:
        details["read_queries"] = int(read_match.group(1))
    write_match = re.search(r"write:\s+(\d+)", output)
    if write_match:
        details["write_queries"] = int(write_match.group(1))
    other_match = re.search(r"other:\s+(\d+)", output)
    if other_match:
        details["other_queries"] = int(other_match.group(1))

    # Latency statistics (ms)
    min_lat = re.search(r"min:\s+([\d\.]+)", output)
    if min_lat:
        details["latency_min_ms"] = float(min_lat.group(1))
    avg_lat = re.search(r"avg:\s+([\d\.]+)", output)
    if avg_lat:
        details["latency_avg_ms"] = float(avg_lat.group(1))
    max_lat = re.search(r"max:\s+([\d\.]+)", output)
    if max_lat:
        details["latency_max_ms"] = float(max_lat.group(1))
    p95_lat = re.search(r"95th percentile:\s+([\d\.]+)", output)
    if p95_lat:
        details["latency_95th_ms"] = float(p95_lat.group(1))
    sum_lat = re.search(r"sum:\s+([\d\.]+)", output)
    if sum_lat:
        details["latency_sum_ms"] = float(sum_lat.group(1))

    # Events
    events_match = re.search(r"total number of events:\s+(\d+)", output)
    if events_match:
        details["total_events"] = int(events_match.group(1))

    return details


def _normalize_host(host: str) -> str:
    """Normalize localhost or empty host to 127.0.0.1 for TCP connection."""
    h = (host or "").strip().lower()
    if h in ("localhost", ""):
        return "127.0.0.1"
    return host


def _build_driver_args(
    db_type: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> list[str]:
    """Construct sysbench database driver CLI flags with normalized host."""
    norm_host = _normalize_host(host)
    db_type_lower = db_type.lower()
    if db_type_lower in ("postgres", "postgresql", "pgsql"):
        return [
            "--db-driver=pgsql",
            f"--pgsql-host={norm_host}",
            f"--pgsql-port={port}",
            f"--pgsql-user={user}",
            f"--pgsql-password={password}",
            f"--pgsql-db={database}",
        ]
    elif db_type_lower == "mysql":
        return [
            "--db-driver=mysql",
            f"--mysql-host={norm_host}",
            f"--mysql-port={port}",
            f"--mysql-user={user}",
            f"--mysql-password={password}",
            f"--mysql-db={database}",
        ]
    else:
        raise ValueError(
            f"Unsupported db_type '{db_type}'. Supported types: 'postgres', 'mysql'."
        )


def run_sysbench_benchmark(
    cfg: DBConfig,
    tables: int = 10,
    table_size: int = 10000,
    threads: int = 4,
    duration: int = 120,
    report_interval: int = 60,
    workdir: str | None = None,
) -> dict[str, Any]:
    """Run sysbench OLTP read/write benchmark on PostgreSQL or MySQL.

    Args:
        cfg: Database configuration.
        tables: Number of tables for the benchmark (default: 10).
        table_size: Number of rows per table (default: 10,000).
        threads: Number of worker threads (default: 4).
        duration: Duration in seconds to run benchmark.
        report_interval: Intermediate report interval in seconds.
        workdir: Directory to save benchmark and prepare logs. Defaults to './log'.

    Returns:
        Dictionary containing benchmark status, tps, qps, duration, threads, tables,
        log_file path, error message (if any), and detailed summary statistics.
    """
    log_dir = workdir if workdir else "./log"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"{int(time.time())}.log")

    try:
        db_type = cfg.db_type.lower()
        if db_type in ("postgres", "postgresql", "pgsql"):
            check_query = "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name LIKE 'sbtest%'"
        elif db_type == "mysql":
            check_query = f"SELECT count(*) FROM information_schema.tables WHERE table_schema = '{cfg.database}' AND table_name LIKE 'sbtest%'"
        else:
            raise ValueError(
                f"Unsupported db_type '{cfg.db_type}'. Supported types: 'postgres', 'mysql'."
            )

        driver_args = _build_driver_args(
            db_type=cfg.db_type,
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
        )

        sysbench_args = driver_args + [
            f"--tables={tables}",
            f"--table-size={table_size}",
            f"--threads={threads}",
        ]

        # Prepare environment variables with passwords
        env = os.environ.copy()
        if cfg.password:
            env["PGPASSWORD"] = cfg.password
            env["MYSQL_PWD"] = cfg.password

        # Check table existence
        conn = get_connection(cfg)
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(check_query)
                row = cursor.fetchone()
                if isinstance(row, dict):
                    count = list(row.values())[0]
                elif row:
                    count = row[0]
                else:
                    count = 0
                count = int(count)
            finally:
                cursor.close()
        finally:
            conn.close()

        # If existing tables are fewer than requested, cleanup and prepare
        if count < tables:
            cleanup_cmd = ["sysbench"] + sysbench_args + ["oltp_read_write", "cleanup"]
            try:
                subprocess.run(
                    cleanup_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    env=env,
                )
            except Exception:
                pass

            prepare_log = os.path.join(log_dir, "sysbench_prepare.log")
            prepare_cmd = ["sysbench"] + sysbench_args + ["oltp_read_write", "prepare"]
            prep_proc = subprocess.run(
                prepare_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                env=env,
            )

            # Segfault resilience: retry prepare with threads=1 and 127.0.0.1
            if prep_proc.returncode in (-11, 139):
                safe_driver_args = _build_driver_args(
                    db_type=cfg.db_type,
                    host="127.0.0.1",
                    port=cfg.port,
                    user=cfg.user,
                    password=cfg.password,
                    database=cfg.database,
                )
                safe_prep_cmd = (
                    ["sysbench"]
                    + safe_driver_args
                    + [
                        f"--tables={tables}",
                        f"--table-size={table_size}",
                        "--threads=1",
                        "oltp_read_write",
                        "prepare",
                    ]
                )
                prep_proc = subprocess.run(
                    safe_prep_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    env=env,
                )

            with open(prepare_log, "w", encoding="utf-8") as f:
                f.write(prep_proc.stdout)
                if prep_proc.stderr:
                    f.write("\n" + prep_proc.stderr)

            if prep_proc.returncode != 0:
                if prep_proc.returncode in (-11, 139):
                    raise RuntimeError(
                        f"Sysbench encountered a segmentation fault (exit code {prep_proc.returncode}) during prepare: {prep_proc.stderr.strip()}"
                    )
                raise RuntimeError(
                    f"sysbench prepare failed with exit code {prep_proc.returncode}: {prep_proc.stderr.strip()}"
                )

        # Run benchmark
        run_cmd = (
            ["sysbench"]
            + sysbench_args
            + [
                f"--time={duration}",
                f"--report-interval={report_interval}",
                "oltp_read_write",
                "run",
            ]
        )
        run_proc = subprocess.run(
            run_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            env=env,
        )

        # Segfault resilience: retry run with threads=1 and 127.0.0.1
        if run_proc.returncode in (-11, 139):
            safe_driver_args = _build_driver_args(
                db_type=cfg.db_type,
                host="127.0.0.1",
                port=cfg.port,
                user=cfg.user,
                password=cfg.password,
                database=cfg.database,
            )
            safe_run_cmd = (
                ["sysbench"]
                + safe_driver_args
                + [
                    f"--tables={tables}",
                    f"--table-size={table_size}",
                    "--threads=1",
                    f"--time={duration}",
                    f"--report-interval={report_interval}",
                    "oltp_read_write",
                    "run",
                ]
            )
            run_proc = subprocess.run(
                safe_run_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                env=env,
            )
            if run_proc.returncode == 0:
                threads = 1

        with open(log_file, "w", encoding="utf-8") as f:
            f.write(run_proc.stdout)
            if run_proc.stderr:
                f.write("\n" + run_proc.stderr)

        if run_proc.returncode != 0:
            if run_proc.returncode in (-11, 139):
                raise RuntimeError(
                    f"Sysbench encountered a segmentation fault (exit code {run_proc.returncode}) during benchmark run: {run_proc.stderr.strip()}"
                )
            raise RuntimeError(
                f"sysbench run failed with exit code {run_proc.returncode}: {run_proc.stderr.strip()}"
            )

        output = run_proc.stdout
        lines = output.splitlines()

        num_intervals = int(duration / report_interval) if report_interval > 0 else 1
        if num_intervals <= 0:
            num_intervals = 1

        qps_candidates: list[float] = []
        for line in lines:
            if "qps" in line:
                parts = line.split()
                if len(parts) > 8:
                    try:
                        qps_candidates.append(float(parts[8]))
                    except ValueError:
                        pass

        if qps_candidates:
            selected = qps_candidates[-num_intervals:]
            divisor = len(selected) if len(selected) < num_intervals else num_intervals
            qps = sum(selected) / float(divisor) if divisor > 0 else 0.0
            tps = float(qps / 20.0)
        else:
            qps = 0.0
            tps = 0.0

        details = _parse_sysbench_summary(output)
        if qps == 0.0:
            qps_summary = re.search(r"queries:\s+\d+\s+\(([\d\.]+)\s+per sec\.\)", output)
            if qps_summary:
                qps = float(qps_summary.group(1))
                tps = float(qps / 20.0)

        return {
            "status": "ok",
            "tps": tps,
            "qps": qps,
            "duration": duration,
            "threads": threads,
            "tables": tables,
            "log_file": log_file,
            "error": None,
            "details": details,
        }

    except Exception as e:
        return {
            "status": "error",
            "tps": 0.0,
            "qps": 0.0,
            "duration": duration,
            "threads": threads,
            "tables": tables,
            "log_file": log_file,
            "error": str(e),
            "details": {},
        }
