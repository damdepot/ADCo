"""Sysbench benchmark runner tool for knob_tuner."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from statistics import median
from typing import Any, Callable

from src.knob_tuner.contracts import SysbenchMeasurement, SysbenchProfile

from .db_connector import DBConfig, checkpoint_database, run_safe_query

_TPS_RE = re.compile(r"transactions:\s+\d+\s+\(([\d.]+)\s+per sec\.\)")
_QPS_RE = re.compile(r"queries:\s+\d+\s+\(([\d.]+)\s+per sec\.\)")
_P95_RE = re.compile(r"95th percentile:\s+([\d.]+)")
_AVG_RE = re.compile(r"avg:\s+([\d.]+)")
_IGNORED_ERRORS_RE = re.compile(r"ignored errors:\s+(\d+)")
_RECONNECTS_RE = re.compile(r"reconnects:\s+(\d+)")

_PGBENCH_TPS_RE = re.compile(r"^tps = ([0-9]+(?:\.[0-9]+)?)", re.MULTILINE)
_PGBENCH_LATENCY_RE = re.compile(
    r"^latency average = ([0-9]+(?:\.[0-9]+)?) ms", re.MULTILINE
)
_PGBENCH_FAILED_RE = re.compile(r"number of failed transactions:\s+(\d+)")

# The pgbench sort/hash workload holds a multi-hundred-MB hash table per client
# at the tuned work_mem, so the client count is capped to keep the analytical
# gate inside the tiny staging memory budget instead of thrashing/OOMing.
_PGBENCH_MAX_CLIENTS = 2

_PREPARE_TIMEOUT_SECONDS = 600

# A freshly bulk-loaded dataset leaves autovacuum churning. Measuring the
# baseline inside that window depresses it and makes every candidate look like a
# win, so an arm that (re)loads the dataset is settled before its warmup. The
# wait is deliberately bounded: autovacuum over a multi-million-row load does not
# finish inside any bounded window, and the A/B/A reversal -- not the settle --
# is what cancels the residual drift. Arms that reuse an already-loaded dataset
# skip the settle entirely (nothing new to settle).
_SETTLE_TIMEOUT_SECONDS = 45
_SETTLE_POLL_SECONDS = 2.0
# A single idle poll can miss an autovacuum worker that has not spawned yet, so
# require the quiescent condition to hold for several consecutive polls and
# never settle before a minimum delay has elapsed.
_SETTLE_MIN_SECONDS = _SETTLE_POLL_SECONDS * 2
_SETTLE_QUIESCENT_POLLS = 2
_AUTOVACUUM_WORKERS_SQL = (
    "SELECT count(*) AS n FROM pg_stat_activity "
    "WHERE backend_type = 'autovacuum worker'"
)

# sysbench tests that only read; every other oltp test mutates the dataset.
_READ_ONLY_SYSBENCH_TESTS = frozenset({"oltp_read_only", "point_select", "select"})
_MUTATING_SQL_RE = re.compile(
    r"\b(insert|update|delete|truncate|merge)\b", re.IGNORECASE
)


def _pgbench_script_mutates(script_path: str) -> bool:
    """Heuristic: does the pgbench script contain a mutating statement?"""
    try:
        with open(script_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return True
    stripped = re.sub(r"--[^\n]*", " ", text)
    return bool(_MUTATING_SQL_RE.search(stripped))


def benchmark_mutates_dataset(benchmark_kind: str, profile: SysbenchProfile) -> bool:
    """Whether the screening benchmark modifies its dataset between runs.

    A read-only benchmark can share one prepared dataset across every arm. A
    mutating benchmark (e.g. sysbench ``oltp_read_write`` inserts/deletes) must
    be re-prepared for each arm, otherwise the reversal arm is measured against
    the bloat the candidate left behind and differs systematically from the cold
    baseline. Unknown cases return True (re-prepare), the conservative choice.
    """
    kind = str(benchmark_kind or "sysbench").strip().lower()
    if kind == "pgbench":
        return _pgbench_script_mutates(_resolve_pgbench_script())
    test = str(getattr(profile, "profile_type", "") or "").strip().lower()
    return test not in _READ_ONLY_SYSBENCH_TESTS


def _settle_after_load(cfg: DBConfig, emit: Callable[[str], None]) -> None:
    """Flush dirty pages and wait for autovacuum to quiesce before measuring.

    PostgreSQL only: forces a CHECKPOINT, then polls until no autovacuum worker
    has been active for ``_SETTLE_QUIESCENT_POLLS`` consecutive polls, and never
    returns before ``_SETTLE_MIN_SECONDS`` have elapsed. Requiring two
    consecutive quiescent polls catches a worker that had not spawned at the
    first poll instant. Bounded by ``_SETTLE_TIMEOUT_SECONDS``. Best-effort
    throughout -- an unsupported engine, permission failure, or timeout must
    never fail the measurement; it only means we proceed without settling.
    """
    if cfg.db_type.lower() not in ("postgres", "postgresql", "pgsql"):
        return
    try:
        checkpoint_database(cfg)
    except Exception:
        pass
    start = time.monotonic()
    deadline = start + _SETTLE_TIMEOUT_SECONDS
    consecutive_quiescent = 0
    while time.monotonic() < deadline:
        try:
            rows = run_safe_query(cfg, _AUTOVACUUM_WORKERS_SQL)
        except Exception:
            return
        if not rows:
            return
        active = int(rows[0].get("n", 0))
        consecutive_quiescent = consecutive_quiescent + 1 if active == 0 else 0
        if (
            consecutive_quiescent >= _SETTLE_QUIESCENT_POLLS
            and time.monotonic() - start >= _SETTLE_MIN_SECONDS
        ):
            return
        time.sleep(_SETTLE_POLL_SECONDS)
    emit("settle: autovacuum still active after timeout; measuring anyway")


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
    prepare_seconds: float = 0.0,
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
        prepare_seconds=prepare_seconds,
        log_file=log_file,
        error=error,
    )


def _failure_reason(returncode: int) -> str:
    """Return a human readable failure reason for a nonzero sysbench return code."""
    if returncode in (-11, 139):
        return f"segmentation fault (exit code {returncode})"
    return f"exit code {returncode}"


def _sysbench_base_args(cfg: DBConfig, profile: SysbenchProfile) -> list[str]:
    """Build the shared sysbench command prefix (driver + dataset + threads)."""
    driver_args = _build_driver_args(
        db_type=cfg.db_type,
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
    )
    return ["sysbench"] + driver_args + [
        f"--tables={profile.tables}",
        f"--table-size={profile.rows_per_table}",
        f"--threads={profile.threads}",
        f"--rand-type={profile.rand_type}",
    ]


def _resolve_pgbench_binary() -> str:
    """Locate the host ``pgbench`` binary, preferring the Homebrew libpq build."""
    configured = os.environ.get("ADCO_PGBENCH_BIN")
    if configured and os.path.exists(configured):
        return configured
    for candidate in (
        "/opt/homebrew/opt/libpq/bin/pgbench",
        "/usr/local/opt/libpq/bin/pgbench",
    ):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("pgbench") or "pgbench"


def _resolve_pgbench_script() -> str:
    """Return the sort/hash pgbench script shipped with the repository."""
    configured = os.environ.get("ADCO_PGBENCH_SCRIPT")
    if configured and os.path.exists(configured):
        return configured
    root = Path(__file__).resolve().parents[3]
    return str(root / "benchmarks" / "tools" / "pgbench" / "sort_hash.pgb")


def _parse_pgbench_output(output: str) -> dict[str, float | int]:
    """Extract TPS, average latency, and failed transaction count from pgbench output."""
    metrics: dict[str, float | int] = {
        "tps": 0.0,
        "latency_avg_ms": 0.0,
        "failed_transactions": 0,
    }
    tps_match = _PGBENCH_TPS_RE.search(output)
    if tps_match:
        metrics["tps"] = float(tps_match.group(1))
    latency_match = _PGBENCH_LATENCY_RE.search(output)
    if latency_match:
        metrics["latency_avg_ms"] = float(latency_match.group(1))
    failed_match = _PGBENCH_FAILED_RE.search(output)
    if failed_match:
        metrics["failed_transactions"] = int(failed_match.group(1))
    return metrics


def derive_screen_profile(
    base: SysbenchProfile,
    target_total_rows: int | None,
    *,
    max_total_rows: int = 5_000_000,
) -> tuple[SysbenchProfile, dict]:
    """Derive a screening profile sized to ``target_total_rows``.

    Returns the (possibly scaled) profile and a metadata dictionary describing
    the derivation. A ``None`` or non-positive target keeps ``base`` unchanged
    and reports ``source="default"``. Targets above ``max_total_rows`` are
    capped.
    """
    if target_total_rows is None or target_total_rows <= 0:
        return base, {
            "source": "default",
            "target_total_rows": 0,
            "total_rows": base.tables * base.rows_per_table,
            "capped": False,
            "tables": base.tables,
            "rows_per_table": base.rows_per_table,
        }

    capped = target_total_rows > max_total_rows
    total = min(target_total_rows, max_total_rows)
    tables = base.tables if base.tables > 0 else 10
    rows_per_table = max(1, total // tables)
    actual_total = tables * rows_per_table
    return (
        base.model_copy(
            update={"tables": tables, "rows_per_table": rows_per_table}
        ),
        {
            "source": "target_rows",
            "target_total_rows": target_total_rows,
            "total_rows": actual_total,
            "capped": capped,
            "tables": tables,
            "rows_per_table": rows_per_table,
        },
    )


def run_sysbench_measurement(
    cfg: DBConfig,
    profile: SysbenchProfile,
    workdir: str | None = None,
    progress: Callable[[str], None] | None = None,
    prepare: bool = True,
) -> SysbenchMeasurement:
    """Run a deterministic, repeated sysbench OLTP measurement.

    The exact same command set is issued for baseline and candidate runs; only
    the database knobs differ externally. Every prepare and run uses the
    profile seed. The dataset is reset once per arm boundary
    (cleanup+prepare), followed by one optional warmup and then all measured
    repetitions back-to-back against the same warm dataset. The final summary
    of each run is parsed (no ``--report-interval``) and results are aggregated
    by median across runs.

    Args:
        cfg: Database configuration.
        profile: Deterministic sysbench parameters.
        workdir: Directory to save the combined benchmark log. Defaults to
            ``logs/sysbench``.
        progress: Optional progress callback.
        prepare: When ``True`` (default) reset the dataset with a cleanup and
            prepare. When ``False`` reuse the already-prepared dataset and skip
            both calls.

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
    prepare_seconds = 0.0

    emit(
        f"Measurement: {profile.tables}×{profile.rows_per_table} rows, "
        f"{profile.threads} threads, {profile.repetitions}×"
        f"{profile.measurement_seconds}s (+{profile.warmup_seconds}s warmup)"
    )

    try:
        common_args = _sysbench_base_args(cfg, profile)
        seed_arg = f"--rand-seed={profile.seed}"
        run_timeout = profile.measurement_seconds + profile.warmup_seconds + 60
        prepare_timeout = max(
            _PREPARE_TIMEOUT_SECONDS,
            (profile.tables * profile.rows_per_table) // 2000,
        )

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

        if prepare:
            # (a) Best-effort cleanup before preparing the initial dataset.
            prepare_start = time.monotonic()
            _exec(
                common_args + [profile.profile_type, "cleanup"],
                timeout=prepare_timeout,
                phase="cleanup",
            )

            # (b) Prepare the initial dataset with the fixed seed.
            prepare_proc = _exec(
                common_args + [seed_arg, profile.profile_type, "prepare"],
                timeout=prepare_timeout,
                phase="prepare",
            )
            prepare_seconds = time.monotonic() - prepare_start
            if prepare_proc.returncode != 0:
                error_message = (
                    f"sysbench prepare failed with {_failure_reason(prepare_proc.returncode)}: "
                    f"{(prepare_proc.stderr or '').strip()}"
                )
                emit(f"Measurement failed: {error_message}")
                return _error_measurement(
                    profile, log_file, error_message, prepare_seconds=prepare_seconds
                )
        else:
            emit("reusing prepared dataset (prepare=False)")

        # (b2) Settle only when this arm actually (re)loaded the dataset. An arm
        # that reuses an already-loaded dataset has no new autovacuum storm to
        # wait out, so settling it would burn the timeout for nothing.
        if prepare:
            emit("settling database (checkpoint + autovacuum quiesce)...")
            _settle_after_load(cfg, emit)

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

        # (d) Measured repetitions against the same warm dataset. The dataset is
        # reset once per arm boundary (cleanup+prepare above), not per repetition:
        # re-preparing between reps discarded the warmup and made every sample a
        # cold start, which dominated the measured spread.
        # ponytail: reset is cleanup+re-prepare once per arm; upgrade to a
        # snapshot/restore only if prepare time starts dominating measured time.
        for repetition in range(1, profile.repetitions + 1):
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
            prepare_seconds=prepare_seconds,
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
        return _error_measurement(
            profile,
            log_file,
            error_message,
            per_run_tps,
            prepare_seconds=prepare_seconds,
        )
    except Exception as e:
        emit(f"Measurement failed: {e}")
        return _error_measurement(
            profile,
            log_file,
            str(e),
            per_run_tps,
            prepare_seconds=prepare_seconds,
        )
    finally:
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                f.write("\n".join(transcript))
        except Exception:  # pragma: no cover - defensive
            pass


def run_pgbench_measurement(
    cfg: DBConfig,
    profile: SysbenchProfile,
    workdir: str | None = None,
    progress: Callable[[str], None] | None = None,
    prepare: bool = True,
) -> SysbenchMeasurement:
    """Run a repeated, host-side pgbench measurement of the sort/hash workload.

    The gate is the same as :func:`run_sysbench_measurement` (deterministic
    repetitions, one settle per arm, median aggregation), but the measured unit
    is the custom ``sort_hash.pgb`` script instead of sysbench OLTP. Host
    pgbench connects to ``cfg.host``/``cfg.port`` exactly as the sysbench runner
    does, so it works unchanged against an isolated staging container.

    ``prepare=True`` reuses the existing sysbench cleanup+prepare commands to
    (re)create the ``sbtest`` dataset the workload reads, then settles the
    database before measuring. The runner is best-effort: any failure yields an
    error :class:`SysbenchMeasurement` and never raises.

    Args:
        cfg: Database configuration.
        profile: Deterministic parameters (repetitions, duration, seed, dataset).
        workdir: Directory to save the combined benchmark log. Defaults to
            ``logs/pgbench``.
        progress: Optional progress callback.
        prepare: When ``True`` (default) reset the dataset with a sysbench
            cleanup and prepare. When ``False`` reuse the already-prepared one.

    Returns:
        A ``SysbenchMeasurement`` whose ``tps`` is the median of the per-run
        ``tps = <x>`` samples.
    """
    emit = progress or (lambda _message: None)
    log_dir = workdir if workdir else os.path.join("logs", "pgbench")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception as e:  # pragma: no cover - defensive
        emit(f"Measurement failed: Failed to create log directory: {e}")
        return _error_measurement(profile, "", f"Failed to create log directory: {e}")

    log_file = os.path.join(
        log_dir, f"pgbench_{int(time.time())}_{uuid.uuid4().hex[:8]}.log"
    )

    transcript: list[str] = []
    per_run_tps: list[float] = []
    per_run_avg: list[float] = []
    total_failed = 0
    current_phase = "initialization"
    prepare_seconds = 0.0

    if cfg.db_type.lower() not in ("postgres", "postgresql", "pgsql"):
        error_message = (
            f"pgbench only supports PostgreSQL, got db_type '{cfg.db_type}'"
        )
        emit(f"Measurement failed: {error_message}")
        return _error_measurement(profile, log_file, error_message)

    script_path = _resolve_pgbench_script()
    if not os.path.isfile(script_path):
        error_message = f"pgbench script not found: {script_path}"
        emit(f"Measurement failed: {error_message}")
        return _error_measurement(profile, log_file, error_message)

    emit(
        f"Measurement: pgbench sort/hash, {profile.tables}×{profile.rows_per_table} "
        f"rows, {profile.repetitions}×{profile.measurement_seconds}s "
        f"(+{profile.warmup_seconds}s warmup)"
    )

    try:
        pgbench_bin = _resolve_pgbench_binary()
        clients = max(1, min(profile.threads, _PGBENCH_MAX_CLIENTS))
        run_timeout = profile.measurement_seconds + profile.warmup_seconds + 120
        prepare_timeout = max(
            _PREPARE_TIMEOUT_SECONDS,
            (profile.tables * profile.rows_per_table) // 2000,
        )

        env = os.environ.copy()
        if cfg.password:
            env["PGPASSWORD"] = cfg.password

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

        if prepare:
            base_args = _sysbench_base_args(cfg, profile)
            prepare_start = time.monotonic()
            _exec(
                base_args + [profile.profile_type, "cleanup"],
                timeout=prepare_timeout,
                phase="cleanup",
            )
            prepare_proc = _exec(
                base_args
                + [f"--rand-seed={profile.seed}", profile.profile_type, "prepare"],
                timeout=prepare_timeout,
                phase="prepare",
            )
            prepare_seconds = time.monotonic() - prepare_start
            if prepare_proc.returncode != 0:
                error_message = (
                    f"sysbench prepare failed with {_failure_reason(prepare_proc.returncode)}: "
                    f"{(prepare_proc.stderr or '').strip()}"
                )
                emit(f"Measurement failed: {error_message}")
                return _error_measurement(
                    profile, log_file, error_message, prepare_seconds=prepare_seconds
                )
        else:
            emit("reusing prepared dataset (prepare=False)")

        if prepare:
            emit("settling database (checkpoint + autovacuum quiesce)...")
            _settle_after_load(cfg, emit)

        def _pgbench_cmd(seconds: int) -> list[str]:
            return [
                pgbench_bin,
                "-n",
                "-f",
                script_path,
                "-c",
                str(clients),
                "-j",
                str(clients),
                "-T",
                str(seconds),
                "-h",
                cfg.host,
                "-p",
                str(cfg.port),
                "-U",
                cfg.user,
                cfg.database,
            ]

        if profile.warmup_seconds > 0:
            warmup_proc = _exec(
                _pgbench_cmd(profile.warmup_seconds),
                timeout=run_timeout,
                phase="warmup",
            )
            if warmup_proc.returncode != 0:
                error_message = (
                    f"pgbench warmup failed with {_failure_reason(warmup_proc.returncode)}: "
                    f"{(warmup_proc.stderr or '').strip()}"
                )
                emit(f"Measurement failed: {error_message}")
                return _error_measurement(profile, log_file, error_message)

        for repetition in range(1, profile.repetitions + 1):
            emit(
                f"rep {repetition}/{profile.repetitions}: "
                f"running {profile.measurement_seconds}s..."
            )
            run_proc = _exec(
                _pgbench_cmd(profile.measurement_seconds),
                timeout=run_timeout,
                phase=f"repetition {repetition}/{profile.repetitions} run",
            )
            if run_proc.returncode != 0:
                error_message = (
                    f"pgbench run failed on repetition {repetition}/"
                    f"{profile.repetitions} with {_failure_reason(run_proc.returncode)}: "
                    f"{(run_proc.stderr or '').strip()}"
                )
                emit(f"Measurement failed: {error_message}")
                return _error_measurement(
                    profile, log_file, error_message, per_run_tps
                )

            metrics = _parse_pgbench_output(run_proc.stdout or "")
            if float(metrics["tps"]) <= 0:
                error_message = (
                    f"pgbench run produced malformed output or zero TPS on repetition "
                    f"{repetition}/{profile.repetitions}"
                )
                emit(f"Measurement failed: {error_message}")
                return _error_measurement(
                    profile, log_file, error_message, per_run_tps
                )

            tps = float(metrics["tps"])
            emit(f"rep {repetition}/{profile.repetitions}: TPS={tps:.2f}")
            per_run_tps.append(tps)
            per_run_avg.append(float(metrics["latency_avg_ms"]))
            total_failed += int(metrics["failed_transactions"])

        measurement = SysbenchMeasurement(
            status="ok",
            tps=_median_or_zero(per_run_tps),
            qps=_median_or_zero(per_run_tps),
            latency_avg_ms=_median_or_zero(per_run_avg),
            latency_p95_ms=_median_or_zero(per_run_avg),
            ignored_errors=0,
            reconnects=0,
            threads=clients,
            tables=profile.tables,
            rows_per_table=profile.rows_per_table,
            duration=profile.measurement_seconds,
            seed=profile.seed,
            repetitions=profile.repetitions,
            per_run_tps=per_run_tps,
            prepare_seconds=prepare_seconds,
            log_file=log_file,
            error=None,
        )

        if total_failed > 0:
            measurement.status = "error"
            measurement.error = f"pgbench recorded {total_failed} failed transactions"

        emit(
            f"Done: median TPS={measurement.tps:.2f}, "
            f"failed_transactions={total_failed}"
        )
        return measurement

    except subprocess.TimeoutExpired as e:
        error_message = f"pgbench {current_phase} timed out: {e}"
        emit(f"Measurement failed: {error_message}")
        return _error_measurement(
            profile,
            log_file,
            error_message,
            per_run_tps,
            prepare_seconds=prepare_seconds,
        )
    except Exception as e:
        emit(f"Measurement failed: {e}")
        return _error_measurement(
            profile,
            log_file,
            str(e),
            per_run_tps,
            prepare_seconds=prepare_seconds,
        )
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
