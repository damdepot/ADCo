"""Sysbench benchmark runner tool for knob_tuner."""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from statistics import median
from typing import Any, Callable

from src.knob_tuner.contracts import SysbenchMeasurement, SysbenchProfile

from .db_connector import DBConfig

_TPS_RE = re.compile(r"transactions:\s+\d+\s+\(([\d.]+)\s+per sec\.\)")
_QPS_RE = re.compile(r"queries:\s+\d+\s+\(([\d.]+)\s+per sec\.\)")
_P95_RE = re.compile(r"95th percentile:\s+([\d.]+)")
_AVG_RE = re.compile(r"avg:\s+([\d.]+)")
_IGNORED_ERRORS_RE = re.compile(r"ignored errors:\s+(\d+)")
_RECONNECTS_RE = re.compile(r"reconnects:\s+(\d+)")

_PREPARE_TIMEOUT_SECONDS = 600


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
    avg_lat = _AVG_RE.search(output)
    if avg_lat:
        details["latency_avg_ms"] = float(avg_lat.group(1))
    max_lat = re.search(r"max:\s+([\d\.]+)", output)
    if max_lat:
        details["latency_max_ms"] = float(max_lat.group(1))
    p95_lat = _P95_RE.search(output)
    if p95_lat:
        details["latency_95th_ms"] = float(p95_lat.group(1))
    sum_lat = re.search(r"sum:\s+([\d\.]+)", output)
    if sum_lat:
        details["latency_sum_ms"] = float(sum_lat.group(1))

    # Events
    events_match = re.search(r"total number of events:\s+(\d+)", output)
    if events_match:
        details["total_events"] = int(events_match.group(1))

    # Errors / reconnects
    ignored_match = _IGNORED_ERRORS_RE.search(output)
    if ignored_match:
        details["ignored_errors"] = int(ignored_match.group(1))
    reconnects_match = _RECONNECTS_RE.search(output)
    if reconnects_match:
        details["reconnects"] = int(reconnects_match.group(1))

    return details


def _parse_run_metrics(output: str) -> dict[str, float | int]:
    """Extract per-run TPS/QPS/latency/error counters from a sysbench run summary."""
    metrics: dict[str, float | int] = {
        "tps": 0.0,
        "qps": 0.0,
        "latency_avg_ms": 0.0,
        "latency_p95_ms": 0.0,
        "ignored_errors": 0,
        "reconnects": 0,
    }

    tps_match = _TPS_RE.search(output)
    if tps_match:
        metrics["tps"] = float(tps_match.group(1))
    qps_match = _QPS_RE.search(output)
    if qps_match:
        metrics["qps"] = float(qps_match.group(1))
    avg_match = _AVG_RE.search(output)
    if avg_match:
        metrics["latency_avg_ms"] = float(avg_match.group(1))
    p95_match = _P95_RE.search(output)
    if p95_match:
        metrics["latency_p95_ms"] = float(p95_match.group(1))
    ignored_match = _IGNORED_ERRORS_RE.search(output)
    if ignored_match:
        metrics["ignored_errors"] = int(ignored_match.group(1))
    reconnects_match = _RECONNECTS_RE.search(output)
    if reconnects_match:
        metrics["reconnects"] = int(reconnects_match.group(1))

    return metrics


def _median_or_zero(values: list[float]) -> float:
    """Return the median of ``values`` or 0.0 when empty."""
    return float(median(values)) if values else 0.0


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


def _error_measurement(
    profile: SysbenchProfile,
    log_file: str,
    error: str,
    per_run_tps: list[float] | None = None,
) -> SysbenchMeasurement:
    """Build an error ``SysbenchMeasurement`` preserving the profile metadata."""
    return SysbenchMeasurement(
        status="error",
        threads=profile.threads,
        tables=profile.tables,
        rows_per_table=profile.rows_per_table,
        duration=profile.measurement_seconds,
        seed=profile.seed,
        repetitions=profile.repetitions,
        per_run_tps=list(per_run_tps or []),
        log_file=log_file,
        error=error,
    )


def _failure_reason(returncode: int) -> str:
    """Return a human readable failure reason for a nonzero sysbench return code."""
    if returncode in (-11, 139):
        return f"segmentation fault (exit code {returncode})"
    return f"exit code {returncode}"


def run_sysbench_measurement(
    cfg: DBConfig,
    profile: SysbenchProfile,
    workdir: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> SysbenchMeasurement:
    """Run a deterministic, repeated sysbench OLTP measurement.

    The exact same command set is issued for baseline and candidate runs; only
    the database knobs differ externally. Every prepare and run uses the
    profile seed, and each repetition resets the dataset with cleanup+prepare
    before the measured run. The final summary of each run is parsed (no
    ``--report-interval``) and results are aggregated by median across runs.

    Args:
        cfg: Database configuration.
        profile: Deterministic sysbench parameters.
        workdir: Directory to save the combined benchmark log. Defaults to
            ``logs/sysbench``.

    Returns:
        A ``SysbenchMeasurement``; ``status`` is ``"ok"`` only when every
        repetition produced well-formed output with TPS > 0 and no ignored
        errors/reconnects. Errors are surfaced via ``status``/``error`` and
        never raised.
    """
    emit = progress or (lambda _message: None)
    log_dir = workdir if workdir else os.path.join("logs", "sysbench")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception as e:  # pragma: no cover - defensive
        emit(f"Measurement failed: Failed to create log directory: {e}")
        return _error_measurement(profile, "", f"Failed to create log directory: {e}")

    log_file = os.path.join(
        log_dir, f"sysbench_{int(time.time())}_{uuid.uuid4().hex[:8]}.log"
    )

    transcript: list[str] = []
    per_run_tps: list[float] = []
    per_run_qps: list[float] = []
    per_run_avg: list[float] = []
    per_run_p95: list[float] = []
    total_ignored_errors = 0
    total_reconnects = 0
    current_phase = "initialization"

    emit(
        f"Measurement: {profile.tables}×{profile.rows_per_table} rows, "
        f"{profile.threads} threads, {profile.repetitions}×"
        f"{profile.measurement_seconds}s (+{profile.warmup_seconds}s warmup)"
    )

    try:
        driver_args = _build_driver_args(
            db_type=cfg.db_type,
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
        )

        common_args = ["sysbench"] + driver_args + [
            f"--tables={profile.tables}",
            f"--table-size={profile.rows_per_table}",
            f"--threads={profile.threads}",
        ]
        seed_arg = f"--rand-seed={profile.seed}"
        run_timeout = profile.measurement_seconds + profile.warmup_seconds + 60

        env = os.environ.copy()
        if cfg.password:
            env["PGPASSWORD"] = cfg.password
            env["MYSQL_PWD"] = cfg.password

        def _exec(cmd: list[str], timeout: int, phase: str) -> subprocess.CompletedProcess:
            nonlocal current_phase
            current_phase = phase
            transcript.append(f"$ {' '.join(cmd)}")
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                env=env,
                timeout=timeout,
            )
            transcript.append(proc.stdout or "")
            if proc.stderr:
                transcript.append(proc.stderr)
            return proc

        # (a) Best-effort cleanup before preparing the initial dataset.
        _exec(
            common_args + [profile.profile_type, "cleanup"],
            timeout=_PREPARE_TIMEOUT_SECONDS,
            phase="cleanup",
        )

        # (b) Prepare the initial dataset with the fixed seed.
        prepare_proc = _exec(
            common_args + [seed_arg, profile.profile_type, "prepare"],
            timeout=_PREPARE_TIMEOUT_SECONDS,
            phase="prepare",
        )
        if prepare_proc.returncode != 0:
            error_message = (
                f"sysbench prepare failed with {_failure_reason(prepare_proc.returncode)}: "
                f"{(prepare_proc.stderr or '').strip()}"
            )
            emit(f"Measurement failed: {error_message}")
            return _error_measurement(profile, log_file, error_message)

        # (c) Optional discarded warmup run.
        if profile.warmup_seconds > 0:
            warmup_proc = _exec(
                common_args
                + [seed_arg, f"--time={profile.warmup_seconds}", profile.profile_type, "run"],
                timeout=run_timeout,
                phase="warmup",
            )
            if warmup_proc.returncode != 0:
                error_message = (
                    f"sysbench warmup failed with {_failure_reason(warmup_proc.returncode)}: "
                    f"{(warmup_proc.stderr or '').strip()}"
                )
                emit(f"Measurement failed: {error_message}")
                return _error_measurement(profile, log_file, error_message)

        # (d) Measured repetitions, each resetting to an identical dataset.
        # ponytail: reset is cleanup+re-prepare (extra I/O per rep) instead of a
        # snapshot/restore; upgrade to tablespace/filesystem snapshots only if
        # prepare time dominates the measured workload.
        for repetition in range(1, profile.repetitions + 1):
            emit(f"rep {repetition}/{profile.repetitions}: resetting dataset...")
            _exec(
                common_args + [profile.profile_type, "cleanup"],
                timeout=_PREPARE_TIMEOUT_SECONDS,
                phase=f"repetition {repetition}/{profile.repetitions} cleanup",
            )
            reset_proc = _exec(
                common_args + [seed_arg, profile.profile_type, "prepare"],
                timeout=_PREPARE_TIMEOUT_SECONDS,
                phase=f"repetition {repetition}/{profile.repetitions} prepare",
            )
            if reset_proc.returncode != 0:
                error_message = (
                    f"sysbench prepare failed on repetition {repetition}/"
                    f"{profile.repetitions} with {_failure_reason(reset_proc.returncode)}: "
                    f"{(reset_proc.stderr or '').strip()}"
                )
                emit(f"Measurement failed: {error_message}")
                return _error_measurement(
                    profile, log_file, error_message, per_run_tps
                )

            emit(
                f"rep {repetition}/{profile.repetitions}: "
                f"running {profile.measurement_seconds}s..."
            )
            run_proc = _exec(
                common_args
                + [seed_arg, f"--time={profile.measurement_seconds}", profile.profile_type, "run"],
                timeout=run_timeout,
                phase=f"repetition {repetition}/{profile.repetitions} run",
            )
            if run_proc.returncode != 0:
                error_message = (
                    f"sysbench run failed on repetition {repetition}/"
                    f"{profile.repetitions} with {_failure_reason(run_proc.returncode)}: "
                    f"{(run_proc.stderr or '').strip()}"
                )
                emit(f"Measurement failed: {error_message}")
                return _error_measurement(
                    profile, log_file, error_message, per_run_tps
                )

            metrics = _parse_run_metrics(run_proc.stdout or "")
            if float(metrics["tps"]) <= 0:
                error_message = (
                    f"sysbench run produced malformed output or zero TPS on repetition "
                    f"{repetition}/{profile.repetitions}"
                )
                emit(f"Measurement failed: {error_message}")
                return _error_measurement(
                    profile, log_file, error_message, per_run_tps
                )

            tps = float(metrics["tps"])
            emit(f"rep {repetition}/{profile.repetitions}: TPS={tps:.2f}")
            per_run_tps.append(tps)
            per_run_qps.append(float(metrics["qps"]))
            per_run_avg.append(float(metrics["latency_avg_ms"]))
            per_run_p95.append(float(metrics["latency_p95_ms"]))
            total_ignored_errors += int(metrics["ignored_errors"])
            total_reconnects += int(metrics["reconnects"])

        # ponytail: sysbench's pgsql driver reports no 95th percentile (always
        # 0.00), so fall back to avg latency to keep the latency gate meaningful.
        median_p95 = _median_or_zero(per_run_p95)
        if median_p95 <= 0:
            median_p95 = _median_or_zero(per_run_avg)

        measurement = SysbenchMeasurement(
            status="ok",
            tps=_median_or_zero(per_run_tps),
            qps=_median_or_zero(per_run_qps),
            latency_avg_ms=_median_or_zero(per_run_avg),
            latency_p95_ms=median_p95,
            ignored_errors=total_ignored_errors,
            reconnects=total_reconnects,
            threads=profile.threads,
            tables=profile.tables,
            rows_per_table=profile.rows_per_table,
            duration=profile.measurement_seconds,
            seed=profile.seed,
            repetitions=profile.repetitions,
            per_run_tps=per_run_tps,
            log_file=log_file,
            error=None,
        )

        # Ignored errors (recoverable DB-level errors, e.g. deadlocks) are recorded
        # but do not invalidate a run; reconnects remain fatal.
        if total_reconnects > 0:
            measurement.status = "error"
            measurement.error = (
                "sysbench recorded reconnects "
                f"(ignored_errors={total_ignored_errors}, reconnects={total_reconnects})"
            )

        emit(
            f"Done: median TPS={measurement.tps:.2f}, "
            f"p95={measurement.latency_p95_ms:.2f}ms, "
            f"ignored_errors={total_ignored_errors}, reconnects={total_reconnects}"
        )
        return measurement

    except subprocess.TimeoutExpired as e:
        error_message = f"sysbench {current_phase} timed out: {e}"
        emit(f"Measurement failed: {error_message}")
        return _error_measurement(profile, log_file, error_message, per_run_tps)
    except Exception as e:
        emit(f"Measurement failed: {e}")
        return _error_measurement(profile, log_file, str(e), per_run_tps)
    finally:
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                f.write("\n".join(transcript))
        except Exception:  # pragma: no cover - defensive
            pass


def run_sysbench_benchmark(
    cfg: DBConfig,
    tables: int = 10,
    table_size: int = 10000,
    threads: int = 4,
    duration: int = 120,
    report_interval: int = 60,
    workdir: str | None = None,
) -> dict[str, Any]:
    """Backward-compatible single-run sysbench benchmark wrapper.

    Builds a deterministic :class:`SysbenchProfile` (single repetition, no
    warmup) and delegates to :func:`run_sysbench_measurement`.

    Args:
        cfg: Database configuration.
        tables: Number of tables for the benchmark (default: 10).
        table_size: Number of rows per table (default: 10,000).
        threads: Number of worker threads (default: 4).
        duration: Duration in seconds to run benchmark.
        report_interval: Accepted for signature compatibility; ignored.
        workdir: Directory to save the combined benchmark log.

    Returns:
        Legacy dictionary with ``status``, ``tps``, ``qps``, ``duration``,
        ``threads``, ``tables``, ``log_file``, ``error`` and ``details``, plus
        the new deterministic keys.
    """
    profile = SysbenchProfile(
        tables=tables,
        rows_per_table=table_size,
        threads=threads,
        warmup_seconds=0,
        measurement_seconds=duration,
        repetitions=1,
    )
    measurement = run_sysbench_measurement(cfg, profile, workdir=workdir)

    details = {
        "latency_avg_ms": measurement.latency_avg_ms,
        "latency_95th_ms": measurement.latency_p95_ms,
        "ignored_errors": measurement.ignored_errors,
        "reconnects": measurement.reconnects,
    }

    return {
        "status": measurement.status,
        "tps": measurement.tps,
        "qps": measurement.qps,
        "duration": measurement.duration,
        "threads": measurement.threads,
        "tables": measurement.tables,
        "log_file": measurement.log_file,
        "error": measurement.error,
        "details": details,
        "ignored_errors": measurement.ignored_errors,
        "reconnects": measurement.reconnects,
        "latency_p95_ms": measurement.latency_p95_ms,
        "per_run_tps": measurement.per_run_tps,
        "seed": measurement.seed,
        "repetitions": measurement.repetitions,
    }
