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
from typing import Callable

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


def _make_exec(
    transcript: list[str],
    env: dict[str, str],
    phase_state: dict[str, str],
) -> Callable[[list[str], int, str], subprocess.CompletedProcess]:
    """Build a logged subprocess runner bound to ``transcript`` and ``env``."""

    def _exec(cmd: list[str], timeout: int, phase: str) -> subprocess.CompletedProcess:
        phase_state["phase"] = phase
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

    return _exec


def run_sysbench_measurement(
    cfg: DBConfig,
    profile: SysbenchProfile,
    workdir: str | None = None,
    progress: Callable[[str], None] | None = None,
    prepare: bool = True,
    on_prepared: Callable[[], None] | None = None,
    on_rep: Callable[[list[float]], bool] | None = None,
    prepare_only: bool = False,
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
        on_prepared: Optional best-effort callback invoked once, right after a
            fresh ``prepare`` and its settle have completed and before the
            warmup/measured runs. Used to snapshot the cleanly loaded dataset.
            A failing callback is reported and otherwise ignored so a snapshot
            failure never fails the measurement.
        on_rep: Optional per-repetition callback receiving the TPS samples
            collected so far; return True to stop the arm early (futility).
            The measurement is then aggregated over the completed reps and the
            short sample is visible via ``per_run_tps``. A failing callback
            never fails the measurement.
        prepare_only: When ``True`` run cleanup + prepare + settle + the
            ``on_prepared`` callback (snapshot commit), then return early
            WITHOUT warmup/reps, with status ``"ok"``, empty per-run lists,
            ``prepare_seconds`` set, and error None. Used to load and
            snapshot the dataset on the preparing container before measuring
            on a fresh container restored from the snapshot.

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
    phase_state: dict[str, str] = {"phase": "initialization"}
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

        _exec = _make_exec(transcript, env, phase_state)

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
            if on_prepared is not None:
                try:
                    on_prepared()
                except Exception as e:
                    emit(
                        "snapshot hook failed (continuing without snapshot): "
                        f"{e}"
                    )

        if prepare_only:
            return SysbenchMeasurement(
                status="ok",
                tps=0.0,
                qps=0.0,
                latency_avg_ms=0.0,
                latency_p95_ms=0.0,
                ignored_errors=0,
                reconnects=0,
                threads=profile.threads,
                tables=profile.tables,
                rows_per_table=profile.rows_per_table,
                duration=profile.measurement_seconds,
                seed=profile.seed,
                repetitions=profile.repetitions,
                per_run_tps=[],
                prepare_seconds=prepare_seconds,
                log_file=log_file,
                error=None,
            )

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

            if on_rep is not None:
                try:
                    if on_rep(list(per_run_tps)):
                        emit(
                            f"rep {repetition}/{profile.repetitions}: "
                            "early stop (futility)"
                        )
                        break
                except Exception as e:
                    emit(f"on_rep hook failed (continuing): {e}")

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
        error_message = f"sysbench {phase_state['phase']} timed out: {e}"
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
    on_prepared: Callable[[], None] | None = None,
    on_rep: Callable[[list[float]], bool] | None = None,
    prepare_only: bool = False,
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
        on_prepared: Optional best-effort callback invoked once, right after a
            fresh ``prepare`` and its settle have completed and before the
            warmup/measured runs (used to snapshot the loaded dataset). A
            failing callback is reported and otherwise ignored.
        on_rep: Optional per-repetition callback receiving the TPS samples
            collected so far; return True to stop the arm early (futility).
            A failing callback never fails the measurement.
        prepare_only: When ``True`` run cleanup + prepare + settle + the
            ``on_prepared`` callback (snapshot commit), then return early
            WITHOUT warmup/reps, with status ``"ok"``, empty per-run lists,
            ``prepare_seconds`` set, and error None.

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
    phase_state: dict[str, str] = {"phase": "initialization"}
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

        _exec = _make_exec(transcript, env, phase_state)

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
            if on_prepared is not None:
                try:
                    on_prepared()
                except Exception as e:
                    emit(
                        "snapshot hook failed (continuing without snapshot): "
                        f"{e}"
                    )

        if prepare_only:
            return SysbenchMeasurement(
                status="ok",
                tps=0.0,
                qps=0.0,
                latency_avg_ms=0.0,
                latency_p95_ms=0.0,
                ignored_errors=0,
                reconnects=0,
                threads=clients,
                tables=profile.tables,
                rows_per_table=profile.rows_per_table,
                duration=profile.measurement_seconds,
                seed=profile.seed,
                repetitions=profile.repetitions,
                per_run_tps=[],
                prepare_seconds=prepare_seconds,
                log_file=log_file,
                error=None,
            )

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

            if on_rep is not None:
                try:
                    if on_rep(list(per_run_tps)):
                        emit(
                            f"rep {repetition}/{profile.repetitions}: "
                            "early stop (futility)"
                        )
                        break
                except Exception as e:
                    emit(f"on_rep hook failed (continuing): {e}")

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
        error_message = f"pgbench {phase_state['phase']} timed out: {e}"
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
