"""Deterministic (non-LLM) validation orchestrator for knob tuning plans.

This module provisions an isolated staging database, measures a baseline,
applies the candidate plan, optionally restarts the staging container, verifies
the resulting settings, measures again, and produces a paired comparison plus a
:class:`ValidationAttestation`. It contains no LLM calls and never raises for
expected failure modes; every outcome is reported through a structured dict.
"""

from __future__ import annotations

import re
import time
from statistics import median
from typing import Any, Callable

from src.knob_tuner.contracts import (
    ApplyMode,
    KnobPlan,
    PairedResult,
    ResourceBudget,
    SysbenchMeasurement,
    SysbenchProfile,
    TuningStatus,
    ValidationAttestation,
)
from src.knob_tuner.tools.benchmark_tools import (
    benchmark_mutates_dataset,
    run_pgbench_measurement,
    run_sysbench_measurement,
)
from src.knob_tuner.tools.db_connector import DBConfig, get_connection, run_safe_query
from src.knob_tuner.tools.db_tools import (
    apply_knobs,
    snapshot_settings,
    verify_active_knobs,
)
from src.knob_tuner.tools.docker_tools import (
    commit_staging_db,
    get_container_host_port,
    recreate_docker_db,
    restart_docker_db,
    start_staging_db,
    stop_staging_db,
)
from src.knob_tuner.tools.knobs import coerce_apply_mode
from src.knob_tuner.tools.run_artifacts import write_artifact
from src.knob_tuner.tools.stats import futility_stop

_RESTART_MODES = (ApplyMode.MANUAL,)

_POSTGRES_TYPES = ("postgres", "postgresql")


class SnapshotRegistry:
    """Run-scoped cache of prepared-dataset snapshot images.

    One image per ``(db_type, db_version, tables, rows_per_table)`` serves every
    validation pass in a run. The first pass that loads a dataset snapshots it;
    later passes boot from the image and skip the multi-million-row prepare, and
    a mutating workload's arms recreate from it instead of re-preparing. The
    caller (workflow) owns the lifecycle and deletes the images at run end.
    """

    def __init__(self) -> None:
        self._images: dict[str, str] = {}

    @staticmethod
    def key_for(
        db_type: str,
        db_version: str | None,
        tables: int,
        rows_per_table: int,
    ) -> str:
        version = str(db_version or "").strip()
        return f"{str(db_type).strip().lower()}:{version}:{int(tables)}x{int(rows_per_table)}"

    def get(self, key: str) -> str | None:
        return self._images.get(key)

    def register(self, key: str, image: str) -> None:
        if key and image:
            self._images[key] = image

    def images(self) -> list[str]:
        return list(self._images.values())

    def to_state(self) -> dict[str, dict[str, str]]:
        """Return a plain-data snapshot (Phase 4.6: no live objects in state)."""
        return {"images": dict(self._images)}

    @classmethod
    def from_state(cls, value: Any) -> SnapshotRegistry:
        """Rebuild a registry from plain state data (never throws)."""
        registry = cls()
        try:
            if isinstance(value, SnapshotRegistry):
                registry._images = dict(value._images)
            elif isinstance(value, dict):
                images = value.get("images", value)
                if isinstance(images, dict):
                    for key, image in images.items():
                        if key and image:
                            registry._images[str(key)] = str(image)
        except Exception:
            pass
        return registry


def _snapshot_image_name(
    run_id: str, tables: int, rows_per_table: int
) -> str:
    """Build a Docker-safe snapshot image tag for one dataset profile."""
    safe = re.sub(r"[^a-z0-9_.-]", "-", str(run_id or "run").lower()).strip("-")
    if not safe:
        safe = "run"
    return f"adco-staging-ready:{safe}-{int(tables)}x{int(rows_per_table)}"


_WAL_METRICS = (
    "wal_records",
    "wal_fpi",
    "wal_bytes",
    "wal_write_time",
    "wal_sync_time",
)
_BGWRITER_METRICS = (
    "checkpoints_timed",
    "checkpoints_req",
    "buffers_checkpoint",
    "buffers_clean",
    "buffers_backend",
)
_DATABASE_METRICS = (
    "blk_read_time",
    "blk_write_time",
    "xact_commit",
    "xact_rollback",
)
_CHECKPOINTER_ALIASES = {
    "num_timed": "checkpoints_timed",
    "num_requested": "checkpoints_req",
    "buffers_written": "buffers_checkpoint",
}


def _as_float(value: Any) -> float:
    """Coerce a metric value to float, treating ``None``/garbage as ``0.0``."""
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _query_first_row(
    cfg: DBConfig, sql: str, params: tuple | None = None
) -> dict[str, Any]:
    """Return the first row of a read-only query as a dict, or ``{}`` on error."""
    try:
        rows = run_safe_query(cfg, sql, params=params)
    except Exception:
        return {}
    if not rows:
        return {}
    row = rows[0]
    return row if isinstance(row, dict) else {}


def _merge_metrics(
    target: dict[str, float], row: dict[str, Any], names: tuple[str, ...]
) -> None:
    """Copy available ``names`` from ``row`` into ``target`` as floats."""
    for name in names:
        if name in row:
            target[name] = _as_float(row.get(name))


def _enable_wal_io_timing(cfg: DBConfig) -> bool:
    """Best-effort enable ``track_wal_io_timing`` on the staging PostgreSQL.

    Uses ``ALTER SYSTEM`` followed by ``pg_reload_conf()`` rather than a
    session-level ``SET``: sysbench opens its own connections, so only a
    cluster-wide setting takes effect for those sessions and populates
    ``pg_stat_wal.wal_write_time``/``wal_sync_time`` and
    ``pg_stat_database.blk_read_time``/``blk_write_time`` for the measurement.

    Returns True when the statements executed. Never raises; a missing
    privilege, an older server, or a connection error simply returns False.
    """
    if cfg.db_type.lower() not in _POSTGRES_TYPES:
        return False
    try:
        conn = get_connection(cfg)
    except Exception:
        return False
    try:
        if hasattr(conn, "autocommit"):
            try:
                conn.autocommit = True
            except Exception:
                pass
        cursor = conn.cursor()
        try:
            cursor.execute("ALTER SYSTEM SET track_wal_io_timing = on;")
            cursor.execute("SELECT pg_reload_conf();")
            if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
                conn.commit()
            return True
        finally:
            cursor.close()
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _collect_pg_write_stats(cfg: DBConfig) -> dict[str, float]:
    """Snapshot PostgreSQL write-path counters as a flat ``{metric: float}`` dict.

    Reads ``pg_stat_wal``, ``pg_stat_bgwriter``, ``pg_stat_database`` (for the
    connected database) and, when present, ``pg_stat_checkpointer`` (PostgreSQL
    17+). Missing views/columns are tolerated so the result works across server
    versions. Never raises.
    """
    if cfg.db_type.lower() not in _POSTGRES_TYPES:
        return {}

    stats: dict[str, float] = {}
    wal_row = _query_first_row(cfg, "SELECT * FROM pg_stat_wal;")
    _merge_metrics(stats, wal_row, _WAL_METRICS)
    bgwriter_row = _query_first_row(cfg, "SELECT * FROM pg_stat_bgwriter;")
    _merge_metrics(stats, bgwriter_row, _BGWRITER_METRICS)
    database_row = _query_first_row(
        cfg,
        "SELECT * FROM pg_stat_database WHERE datname = %s;",
        params=(cfg.database,),
    )
    _merge_metrics(stats, database_row, _DATABASE_METRICS)
    checkpointer_row = _query_first_row(cfg, "SELECT * FROM pg_stat_checkpointer;")
    for source, target in _CHECKPOINTER_ALIASES.items():
        if target not in stats and source in checkpointer_row:
            stats[target] = _as_float(checkpointer_row.get(source))
    return stats


def _safe_ratio(numerator: float, denominator: float) -> float:
    """Divide, returning ``0.0`` when the denominator is zero."""
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _with_derived(delta: dict[str, float]) -> dict[str, float]:
    """Attach derived write-path ratios to a delta dict."""
    delta["wal_bytes_per_commit"] = _safe_ratio(
        delta.get("wal_bytes", 0.0), delta.get("xact_commit", 0.0)
    )
    return delta


def _snapshot_delta(
    before: dict[str, float], after: dict[str, float]
) -> dict[str, float]:
    """Compute ``after - before`` per metric, tolerating missing keys."""
    delta = {
        key: _as_float(after.get(key, 0.0)) - _as_float(before.get(key, 0.0))
        for key in set(before) | set(after)
    }
    return _with_derived(delta)


def _subtract_deltas(
    after: dict[str, float], before: dict[str, float]
) -> dict[str, float]:
    """Compute the net difference between two delta dicts."""
    keys = (set(after) | set(before)) - {"wal_bytes_per_commit"}
    net = {
        key: _as_float(after.get(key, 0.0)) - _as_float(before.get(key, 0.0))
        for key in keys
    }
    return _with_derived(net)


def _finalize(
    status: TuningStatus,
    attestation: ValidationAttestation | None,
    paired: PairedResult | None,
    reasons: list[str],
    staging_db_config: DBConfig | None,
    artifacts: dict[str, Any],
    wal_evidence: dict[str, Any] | None = None,
    baseline_reversal: dict[str, Any] | None = None,
    ignored_errors: dict[str, Any] | None = None,
    stopped_early: bool = False,
) -> dict[str, Any]:
    """Build the JSON-serializable return payload (except the DBConfig)."""
    return {
        "status": status.value,
        "attestation": attestation.model_dump() if attestation is not None else None,
        "paired": paired.model_dump() if paired is not None else None,
        "reasons": reasons,
        "staging_db_config": staging_db_config,
        "artifacts": artifacts,
        "wal_evidence": wal_evidence,
        "baseline_reversal": baseline_reversal,
        "ignored_errors": ignored_errors,
        "stopped_early": stopped_early,
    }


def _build_attestation(
    *,
    run_id: str,
    db_type: str,
    database: str,
    staging_cfg: DBConfig,
    budget: ResourceBudget,
    profile: SysbenchProfile,
    plan: KnobPlan,
    settings_before: dict[str, str],
    settings_after: dict[str, str],
    verified_knobs: list[dict[str, Any]],
    artifacts: dict[str, Any],
    status: TuningStatus,
    attempt: int,
) -> ValidationAttestation:
    return ValidationAttestation(
        run_id=run_id,
        database_identity=f"{db_type}://{staging_cfg.host}:{staging_cfg.port}/{database}",
        db_engine=db_type,
        resource_budget=budget,
        profile_hash=profile.profile_hash(),
        seed=profile.seed,
        plan_hash=plan.plan_hash(),
        settings_before=settings_before,
        settings_after=settings_after,
        verified_knobs=verified_knobs,
        benchmark_artifacts={
            "baseline": artifacts.get("baseline", ""),
            "tuned": artifacts.get("tuned", ""),
            "baseline_reversal": artifacts.get("baseline_reversal", ""),
            "baseline_reversal_status": artifacts.get("baseline_reversal_status", ""),
            "ignored_errors": artifacts.get("ignored_errors", ""),
        },
        status=status,
        attempts=attempt,
    )


def _pooled_baseline(
    first: SysbenchMeasurement, second: SysbenchMeasurement
) -> SysbenchMeasurement:
    """Merge two baseline measurements into a single pooled reference.

    The per-run TPS samples from both baseline arms are concatenated so the
    pooled median reflects the whole A1/A2 baseline window. A monotonic
    page-cache warming trend across the three arms then cancels to first order
    instead of letting a later, warmer candidate beat a single cold baseline.

    Pooling never manufactures a healthy reference: if either arm is not ok the
    first arm is returned with a non-ok status and an explicit error, so the
    paired comparison reports the missing evidence instead of resurrecting a
    failed baseline as a clean measurement.
    """
    unhealthy = [
        label
        for label, measurement in (("A1", first), ("A2", second))
        if measurement.status != "ok"
    ]
    if unhealthy:
        return first.model_copy(
            update={
                "status": "error",
                "error": "cannot pool non-ok baseline arms: " + ", ".join(unhealthy),
            }
        )
    pooled_tps = list(first.per_run_tps) + list(second.per_run_tps)
    return first.model_copy(
        update={
            "status": "ok",
            "tps": float(median(pooled_tps)) if pooled_tps else 0.0,
            "qps": (first.qps + second.qps) / 2.0,
            "latency_avg_ms": (first.latency_avg_ms + second.latency_avg_ms) / 2.0,
            "latency_p95_ms": (first.latency_p95_ms + second.latency_p95_ms) / 2.0,
            "ignored_errors": first.ignored_errors + second.ignored_errors,
            "reconnects": first.reconnects + second.reconnects,
            "per_run_tps": pooled_tps,
            "error": None,
        }
    )


_IGNORED_ERROR_ABS_TOLERANCE = 5
_IGNORED_ERROR_REL_TOLERANCE = 0.25


def _ignored_error_mismatch(
    *, baseline: int, reversal: int | None, tuned: int
) -> tuple[bool, str]:
    """Flag a materially different ignored-error rate between the arms.

    Ignored errors are recoverable database errors sysbench swallows; when the
    baseline and tuned arms see materially different counts the error path
    itself changes the work measured, so the arms are no longer like-for-like
    and the comparison must not be reported as a clean win. Returns
    ``(mismatch, reason)``.
    """
    arms = {"baseline A1": int(baseline)}
    if reversal is not None:
        arms["baseline A2"] = int(reversal)
    tuned_count = int(tuned)
    mismatches: list[str] = []
    for label, count in arms.items():
        difference = abs(count - tuned_count)
        larger = max(count, tuned_count, 1)
        if (
            difference >= _IGNORED_ERROR_ABS_TOLERANCE
            and difference / larger >= _IGNORED_ERROR_REL_TOLERANCE
        ):
            mismatches.append(f"{label}={count} vs tuned={tuned_count} (Δ={difference})")
    if not mismatches:
        return False, ""
    return (
        True,
        "ignored-error mismatch between arms ("
        + "; ".join(mismatches)
        + "); comparison may not be like-for-like",
    )


def _error_rate(errors: int, measurement: Any) -> float:
    """Return the ignored-error rate for a measurement arm.

    Rate is ``errors / max(1.0, total_transactions)`` where
    ``total_transactions = sum(per_run_tps) * seconds`` with ``seconds``
    taken from ``measurement.duration`` when positive, else ``10.0``.
    Absolute counts punish higher throughput (more total transactions), so
    rates keep the arms comparable. Defensive: missing attributes or bad
    values yield ``0.0`` and this helper never raises.
    """
    try:
        try:
            count = int(errors)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        if count <= 0:
            return 0.0
        if measurement is None:
            return 0.0
        try:
            per_run = getattr(measurement, "per_run_tps", None)
        except Exception:
            return 0.0
        try:
            total = (
                float(sum(float(x) for x in per_run))
                if per_run
                else 0.0
            )
        except (TypeError, ValueError):
            return 0.0
        try:
            seconds = float(getattr(measurement, "duration", 10.0))
        except (TypeError, ValueError):
            seconds = 10.0
        if not seconds or seconds <= 0:
            seconds = 10.0
        denom = total * seconds
        if denom <= 0:
            denom = 1.0
        return max(0.0, float(count) / max(1.0, denom))
    except Exception:
        return 0.0


def _select_runner(benchmark_kind: str) -> Callable[..., SysbenchMeasurement]:
    """Return the measurement runner for ``benchmark_kind`` (default sysbench)."""
    if str(benchmark_kind or "").strip().lower() == "pgbench":
        return run_pgbench_measurement
    return run_sysbench_measurement


def _benchmark_label(benchmark_kind: str) -> str:
    """Return the artifact filename label for ``benchmark_kind``."""
    if str(benchmark_kind or "").strip().lower() == "pgbench":
        return "pgbench"
    return "sysbench"


def _measure_baseline_reversal(
    *,
    run_dir: str,
    plan: KnobPlan,
    mode: ApplyMode,
    container: str | None,
    staging_cfg: DBConfig,
    profile: SysbenchProfile,
    workdir: str | None,
    settings_before: dict[str, str],
    runner: Callable[..., SysbenchMeasurement],
    benchmark_kind: str,
    prepare: bool,
    emit: Callable[[str], None],
    timings: dict[str, float],
    artifacts: dict[str, Any],
) -> SysbenchMeasurement:
    """Restore the pre-plan settings and re-measure the baseline (A2).

    Re-applies the values captured in ``settings_before``, restarts staging when
    the plan used restart-required semantics, then measures the restored
    baseline. ``prepare`` mirrors whether the benchmark mutates its dataset, so a
    mutating workload is re-prepared (A2 then sees the same fresh dataset as A1
    instead of the candidate's bloat) and a read-only workload reuses the shared
    dataset. Any failure raises so the
    caller can fall back to the single A1 baseline; a reversal that could not be
    measured must never fail a run.
    """
    revert_knobs: list[dict[str, Any]] = []
    for knob in plan.knobs:
        pre_value = settings_before.get(knob.name)
        if pre_value is None:
            raise RuntimeError(f"no pre-plan value captured for '{knob.name}'")
        revert_knobs.append(
            {
                "name": knob.name,
                "value": pre_value,
                "scope": knob.scope,
                "restart_required": knob.restart_required,
            }
        )

    emit(f"reverting {len(revert_knobs)} knobs for baseline reversal...")
    t0 = time.monotonic()
    revert_results = apply_knobs(revert_knobs, staging_cfg, dry_run=False, mode=mode)
    timings["revert_seconds"] = round(time.monotonic() - t0, 3)
    failed_reverts = [result for result in revert_results if result.get("status") == "failed"]
    if failed_reverts:
        names = ", ".join(str(result.get("knob")) for result in failed_reverts)
        raise RuntimeError(f"revert failed for: {names}")

    if mode in _RESTART_MODES:
        emit("restarting staging database after baseline revert...")
        t0 = time.monotonic()
        ok, message = restart_docker_db(container, db_type=staging_cfg.db_type)
        timings["revert_restart_seconds"] = round(time.monotonic() - t0, 3)
        if not ok:
            raise RuntimeError(f"staging restart after revert failed: {message}")
        internal_port = 3306 if "my" in staging_cfg.db_type.lower() else 5432
        try:
            staging_cfg.port = get_container_host_port(container, internal_port)
        except Exception:
            pass

    emit("running baseline reversal measurement...")
    t0 = time.monotonic()
    reversal = runner(staging_cfg, profile, workdir, progress=emit, prepare=prepare)
    timings["baseline_reversal_seconds"] = round(time.monotonic() - t0, 3)
    if reversal.status != "ok":
        raise RuntimeError(
            f"baseline reversal measurement status is '{reversal.status}'"
        )
    artifacts["baseline_reversal"] = write_artifact(
        run_dir, f"{_benchmark_label(benchmark_kind)}-baseline-reversal", reversal.model_dump()
    )
    return reversal


def _measure_shared_baseline(
    *,
    run_id: str,
    run_dir: str,
    budget: ResourceBudget,
    profile: SysbenchProfile,
    db_type: str,
    db_version: str | None,
    database: str,
    runner: Callable[..., SysbenchMeasurement],
    label: str,
    snapshot: SnapshotRegistry | None,
    snapshot_key: str,
    emit: Callable[[str], None],
    workdir: str | None = None,
) -> dict[str, Any]:
    """Provision, prepare, and measure the shared baseline once per run.

    The cleanly prepared dataset is snapshotted (when supported) so every
    later candidate boots from the image instead of re-preparing. Returns a
    dict with ``status`` (``"ok"``/``"error"``), ``baseline`` (the
    :class:`SysbenchMeasurement` or ``None``), ``reasons``, ``artifacts``,
    ``snapshot_key`` and ``snapshot_image``.
    """
    reasons: list[str] = []
    artifacts: dict[str, Any] = {}
    timings: dict[str, float] = {}
    container: str | None = None
    try:
        emit(
            f"provisioning shared-baseline {db_type} {db_version or 'default'} "
            f"({budget.cpu_cores} CPU / {budget.memory_gb} GB)..."
        )
        try:
            t0 = time.monotonic()
            container, staging_cfg = start_staging_db(
                db_type=db_type,
                db_version=db_version,
                budget=budget,
                database=database,
                base_image=None,
            )
            timings["provision_seconds"] = round(time.monotonic() - t0, 3)
        except Exception as e:
            emit(f"shared baseline provisioning failed: {e}")
            return {
                "status": "error",
                "baseline": None,
                "reasons": [f"environment error: shared baseline provisioning failed: {e}"],
                "artifacts": artifacts,
                "snapshot_key": snapshot_key,
                "snapshot_image": None,
            }
        emit(f"shared baseline staging ready on {staging_cfg.host}:{staging_cfg.port}")

        if _enable_wal_io_timing(staging_cfg):
            emit("enabled track_wal_io_timing on staging")

        image_name = ""
        baseline_container = container
        if snapshot is not None and snapshot_key:
            image_name = _snapshot_image_name(
                run_id, profile.tables, profile.rows_per_table
            )

            def _on_prepared() -> None:
                ok, message = commit_staging_db(baseline_container, image_name)
                if ok:
                    snapshot.register(snapshot_key, image_name)
                    emit(f"snapshot ready: {image_name}")
                else:
                    emit(
                        "snapshot commit failed (continuing without "
                        f"snapshot): {message}"
                    )

            on_prepared = _on_prepared
        else:
            on_prepared = None

        emit("running shared baseline measurement...")
        t0 = time.monotonic()
        baseline = runner(
            staging_cfg,
            profile,
            workdir,
            progress=emit,
            prepare=True,
            on_prepared=on_prepared,
        )
        timings["baseline_seconds"] = round(time.monotonic() - t0, 3)
        timings["prepare_seconds"] = baseline.prepare_seconds
        artifacts["timings"] = timings
        artifacts["shared_baseline"] = write_artifact(
            run_dir, f"{label}-shared-baseline", baseline.model_dump()
        )
        if baseline.status != "ok":
            reasons.append(
                f"shared baseline measurement status is '{baseline.status}'"
                + (f": {baseline.error}" if baseline.error else "")
            )
            return {
                "status": "error",
                "baseline": baseline,
                "reasons": reasons,
                "artifacts": artifacts,
                "snapshot_key": snapshot_key,
                "snapshot_image": snapshot.get(snapshot_key)
                if snapshot is not None and snapshot_key
                else None,
            }
        emit(
            f"shared baseline done: median TPS={baseline.tps:.2f} "
            f"({len(baseline.per_run_tps)} reps)"
        )
        return {
            "status": "ok",
            "baseline": baseline,
            "reasons": reasons,
            "artifacts": artifacts,
            "snapshot_key": snapshot_key,
            "snapshot_image": snapshot.get(snapshot_key)
            if snapshot is not None and snapshot_key
            else None,
        }
    finally:
        if container:
            emit("tearing down shared-baseline staging container...")
            try:
                stop_staging_db(container)
            except Exception:
                pass


def validate_plan(
    *,
    run_id: str,
    run_dir: str,
    plan: KnobPlan,
    budget: ResourceBudget,
    profile: SysbenchProfile,
    db_type: str,
    db_version: str | None,
    database: str,
    apply_mode: ApplyMode,
    dry_run: bool = False,
    workdir: str | None = None,
    attempt: int = 1,
    reuse_dataset: bool = True,
    benchmark_kind: str = "sysbench",
    progress: Callable[[str], None] | None = None,
    snapshot: SnapshotRegistry | None = None,
    reversal: bool = True,
    shared_baseline: SysbenchMeasurement | dict[str, Any] | None = None,
    baseline_only: bool = False,
    early_stop_min_reps: int | None = None,
) -> dict[str, Any]:
    """Validate a knob plan end-to-end against an isolated staging database.

    Args:
        run_id: Identifier for the run.
        run_dir: Directory where validation artifacts are written.
        plan: The candidate :class:`KnobPlan`.
        budget: Resource budget enforced on the staging container.
        profile: Deterministic sysbench profile for baseline/tuned runs.
        db_type: Database engine ('postgres' or 'mysql').
        db_version: Optional database version string.
        database: Database name to create in staging.
        apply_mode: How knobs should be applied.
        dry_run: When True, skip all provisioning/SQL/restarts.
        workdir: Optional sysbench log directory.
        attempt: Attempt number used in the attestation artifact name.
        reuse_dataset: Retained for call compatibility and no longer affects the
            dataset. Whether each arm is re-prepared depends on the benchmark:
            a mutating workload (sysbench ``oltp_read_write``) is re-prepared per
            arm so the reversal does not see the candidate's bloat, while a
            read-only benchmark shares one prepared dataset across arms.
        benchmark_kind: Measurement runner to use, ``"sysbench"`` (default) or
            ``"pgbench"`` (the sort/hash analytical workload).
        snapshot: Optional run-scoped prepared-dataset snapshot registry. When
            provided, the first pass snapshots the cleanly prepared dataset and
            later passes boot from it, skipping the (multi-million-row) prepare
            and settle. Mutating arms recreate the staging container from the
            snapshot and re-apply their knobs instead of re-preparing.
        reversal: When True (default) measure the A/B/A baseline reversal. Set
            False for cheap screening passes; confirmation keeps the reversal.
            Ignored when ``shared_baseline`` is given (no per-candidate
            reversal; the shared baseline is measured once).
        shared_baseline: Optional already-measured baseline
            :class:`SysbenchMeasurement` (or its dict form) reused across
            candidates. When given, the internal baseline and reversal are
            skipped and ``paired`` is evaluated against it.
        baseline_only: When True, only provision, prepare, and measure the
            baseline (snapshotting it for later candidates); no plan is
            applied and no tuned/reversal arms run. Returns a dict with
            ``status`` (``"ok"``/``"error"``), ``baseline``, ``reasons``,
            ``artifacts``, ``snapshot_key`` and ``snapshot_image``.
        early_stop_min_reps: When set (and a shared baseline is available),
            stop the tuned arm early for futility once this many reps ran and
            the 95% upper bound is below zero. Winners always run the full
            repetition count, so early stopping cannot manufacture a PASS.

    Returns:
        A structured dict with ``status``, ``attestation``, ``paired``,
        ``reasons``, ``staging_db_config``, ``artifacts``, ``wal_evidence``,
        ``baseline_reversal`` and ``ignored_errors``.
    """
    emit = progress or (lambda _message: None)
    # Preserve the request before the local A2 measurement variable shadows it.
    reversal_enabled = bool(reversal)

    if dry_run:
        emit("dry-run: validation skipped")
        return _finalize(
            TuningStatus.INCONCLUSIVE,
            None,
            None,
            ["dry-run: validation skipped (no mutations)"],
            None,
            {},
        )

    mode = coerce_apply_mode(apply_mode)
    runner = _select_runner(benchmark_kind)
    label = _benchmark_label(benchmark_kind)
    # Read-only benchmarks share one prepared dataset across arms; a mutating
    # benchmark must be re-prepared per arm or the reversal sees the candidate's
    # bloat. Re-preparing a multi-million-row dataset is the dominant gate cost,
    # so only do it when the workload actually mutates the data.
    mutates_dataset = benchmark_mutates_dataset(benchmark_kind, profile)
    # Snapshotting relies on PGDATA living on the container's writable layer
    # (see docker_tools.start_staging_db); only PostgreSQL is wired for that, so
    # other engines keep the plain re-prepare behavior.
    snapshot_supported = str(db_type).strip().lower() in _POSTGRES_TYPES
    snapshot_key = (
        SnapshotRegistry.key_for(
            db_type, db_version, profile.tables, profile.rows_per_table
        )
        if snapshot is not None and snapshot_supported
        else ""
    )
    snapshot_image = snapshot.get(snapshot_key) if snapshot is not None and snapshot_key else None

    if isinstance(shared_baseline, dict):
        try:
            shared_baseline = SysbenchMeasurement.model_validate(shared_baseline)
        except Exception:
            shared_baseline = None

    if baseline_only:
        return _measure_shared_baseline(
            run_id=run_id,
            run_dir=run_dir,
            budget=budget,
            profile=profile,
            db_type=db_type,
            db_version=db_version,
            database=database,
            runner=runner,
            label=label,
            snapshot=snapshot,
            snapshot_key=snapshot_key,
            emit=emit,
        )

    emit(
        f"benchmark runner: {label} "
        f"(re-prepare per arm: {'yes' if mutates_dataset else 'no'}"
        + (", snapshot: reuse" if snapshot_image else "")
        + ")"
    )

    reasons: list[str] = []
    artifacts: dict[str, Any] = {}
    timings: dict[str, float] = {}
    container: str | None = None
    staging_cfg: DBConfig | None = None
    baseline: SysbenchMeasurement | None = None
    tuned: SysbenchMeasurement | None = None
    paired: PairedResult | None = None
    wal_before: dict[str, float] = {}
    wal_tuned_before: dict[str, float] = {}
    wal_after: dict[str, float] = {}
    wal_evidence: dict[str, Any] | None = None
    settings_before: dict[str, str] = {}
    settings_after: dict[str, str] = {}
    verification: dict[str, Any] = {"knobs": [], "all_verified": False, "status": "error"}
    applied_results: list[dict[str, Any]] = []
    failed_applications: list[dict[str, Any]] = []
    restart_failed = False
    ignored_error_mismatch = False
    status = TuningStatus.FAIL
    baseline_reversal: dict[str, Any] = {
        "measured": False,
        "reason": "baseline reversal was not attempted",
    }
    ignored_errors: dict[str, Any] | None = None
    wal_baseline_after: dict[str, float] = {}

    def _restore_from_snapshot(reason: str) -> bool:
        """Recreate the staging container from the current snapshot image.

        Returns True when a fresh container was started from the snapshot;
        False when no snapshot is available or the recreate failed, in which
        case the caller falls back to re-preparing the dataset in place.
        """
        nonlocal container, staging_cfg
        image = (
            snapshot.get(snapshot_key)
            if snapshot is not None and snapshot_key
            else None
        )
        if not image or not container:
            return False
        emit(f"restoring prepared dataset from snapshot ({reason})...")
        t0 = time.monotonic()
        ok, new_container, new_cfg = recreate_docker_db(
            container,
            db_type=db_type,
            db_version=db_version,
            database=database,
            budget=budget,
            base_image=image,
        )
        if ok and new_cfg is not None:
            container = new_container
            staging_cfg = new_cfg
            timings["restore_seconds"] = round(
                timings.get("restore_seconds", 0.0)
                + (time.monotonic() - t0),
                3,
            )
            return True
        emit(f"snapshot restore failed, re-preparing instead: {new_cfg}")
        return False

    try:
        # 1. Provision the isolated staging container (also verifies resources).
        emit(
            f"provisioning staging {db_type} {db_version or 'default'} "
            f"({budget.cpu_cores} CPU / {budget.memory_gb} GB)..."
        )
        try:
            t0 = time.monotonic()
            container, staging_cfg = start_staging_db(
                db_type=db_type,
                db_version=db_version,
                budget=budget,
                database=database,
                base_image=snapshot_image,
            )
            timings["provision_seconds"] = round(time.monotonic() - t0, 3)
        except Exception as e:
            emit(f"staging provisioning failed: {e}")
            return _finalize(
                TuningStatus.FAIL,
                None,
                None,
                [f"environment error: staging provisioning failed: {e}"],
                None,
                {},
            )
        emit(f"staging ready on {staging_cfg.host}:{staging_cfg.port}")

        # Populate WAL I/O timings for all sessions (best effort).
        if _enable_wal_io_timing(staging_cfg):
            emit("enabled track_wal_io_timing on staging")

        names = [knob.name for knob in plan.knobs]

        # 2. Snapshot settings before any mutation.
        settings_before = snapshot_settings(staging_cfg, names)

        # 3. Baseline measurement. When a snapshot is reused the dataset is
        #    already loaded and settled, so the prepare/settle is skipped; when
        #    loading fresh, the cleanly prepared dataset is snapshotted for the
        #    rest of the run via the on_prepared hook. When a shared baseline
        #    was measured once for the whole run, it is reused directly and no
        #    per-candidate baseline (or reversal) runs at all.
        baseline_on_prepared: Callable[[], None] | None = None
        if shared_baseline is not None:
            baseline = shared_baseline
            emit(
                "reusing shared baseline "
                f"(median TPS={baseline.tps:.2f}, "
                f"{len(baseline.per_run_tps)} reps)"
            )
            artifacts["baseline"] = write_artifact(
                run_dir, f"{label}-baseline-shared", baseline.model_dump()
            )
            # No baseline window on this container; the tuned window below is
            # compared against the shared baseline's own evidence.
            wal_baseline_after = {}
        else:
            if snapshot is not None and snapshot_key and not snapshot_image:
                image_name = _snapshot_image_name(
                    run_id, profile.tables, profile.rows_per_table
                )
                baseline_container = container

                def baseline_on_prepared() -> None:
                    ok, message = commit_staging_db(baseline_container, image_name)
                    if ok:
                        snapshot.register(snapshot_key, image_name)
                        emit(f"snapshot ready: {image_name}")
                    else:
                        emit(
                            "snapshot commit failed (continuing without "
                            f"snapshot): {message}"
                        )

            emit("running baseline measurement...")
            wal_before = _collect_pg_write_stats(staging_cfg)
            t0 = time.monotonic()
            baseline = runner(
                staging_cfg,
                profile,
                workdir,
                progress=emit,
                prepare=not bool(snapshot_image),
                on_prepared=baseline_on_prepared,
            )
            timings["baseline_seconds"] = round(time.monotonic() - t0, 3)
            timings["prepare_seconds"] = baseline.prepare_seconds
            artifacts["baseline"] = write_artifact(
                run_dir, f"{label}-baseline", baseline.model_dump()
            )
            # Baseline window must be measured on one container before any restore.
            wal_baseline_after = _collect_pg_write_stats(staging_cfg)

        # 3b. A mutating workload must start the tuned arm from the same clean
        #     dataset the baseline saw. Restore it from the snapshot instead of
        #     re-running the (multi-minute) prepare in place.
        tuned_restored = False
        if (
            mutates_dataset
            and snapshot is not None
            and snapshot_key
            and snapshot.get(snapshot_key)
        ):
            tuned_restored = _restore_from_snapshot("tuned arm")

        # 4. Apply the plan using the requested mode.
        raw_knobs = [
            {
                "name": knob.name,
                "value": knob.value,
                "scope": knob.scope,
                "restart_required": knob.restart_required,
            }
            for knob in plan.knobs
        ]
        emit(f"applying {len(plan.knobs)} knobs ({mode.value})...")
        t0 = time.monotonic()
        applied_results = apply_knobs(raw_knobs, staging_cfg, dry_run=False, mode=mode)
        timings["apply_seconds"] = round(time.monotonic() - t0, 3)
        failed_applications = [
            result for result in applied_results if result.get("status") == "failed"
        ]
        applied_count = sum(
            1 for result in applied_results if result.get("status") == "applied"
        )
        emit(f"applied {applied_count}, failed {len(failed_applications)}")

        # 5. Restart the isolated staging DB to verify restart-required settings
        #    and refresh the port. Production is never restarted here.
        if mode in _RESTART_MODES:
            emit("restarting staging database...")
            t0 = time.monotonic()
            ok, message = restart_docker_db(container, db_type=db_type)
            timings["restart_seconds"] = round(time.monotonic() - t0, 3)
            if not ok:
                restart_failed = True
                reasons.append(f"staging restart failed: {message}")
                emit(f"staging restart failed: {message}")
            else:
                internal_port = 3306 if "my" in db_type.lower() else 5432
                try:
                    staging_cfg.port = get_container_host_port(container, internal_port)
                except Exception:
                    pass

        # 6. Verify the active settings.
        verify_input = [{"name": knob.name, "value": knob.value} for knob in plan.knobs]
        emit(f"verifying {len(plan.knobs)} knobs...")
        verification = verify_active_knobs(staging_cfg, verify_input)
        verified_knobs = verification.get("knobs", [])
        all_verified = bool(verification.get("all_verified", False)) and all(
            entry.get("status") == "VERIFIED" for entry in verified_knobs
        )
        emit(f"verification all_verified={all_verified}")

        settings_after = snapshot_settings(staging_cfg, names)
        artifacts["settings_after"] = write_artifact(
            run_dir,
            "settings-after",
            {"settings": settings_after, "verify": verification},
        )

        # 7. Tuned measurement. A mutating workload restores the dataset from
        #    the snapshot above (or re-prepares on fallback); a read-only
        #    benchmark runs against the exact dataset the baseline used. In
        #    shared-baseline mode the container boots from the snapshot, so no
        #    prepare is needed unless no snapshot image exists.
        emit("running tuned measurement...")
        wal_tuned_before = _collect_pg_write_stats(staging_cfg)
        if shared_baseline is not None and not snapshot_image:
            tuned_prepare = True
        else:
            tuned_prepare = mutates_dataset and not tuned_restored
        on_rep_hook: Callable[[list[float]], bool] | None = None
        stopped_early = False
        if (
            early_stop_min_reps is not None
            and shared_baseline is not None
            and list(shared_baseline.per_run_tps)
        ):
            shared_tps = [float(x) for x in shared_baseline.per_run_tps]
            min_reps = max(2, int(early_stop_min_reps))
            hook_fired: list[bool] = []

            def on_rep_hook(tuned_so_far: list[float]) -> bool:
                stop, _stats = futility_stop(shared_tps, tuned_so_far, min_reps)
                if stop:
                    hook_fired.append(True)
                return stop

        t0 = time.monotonic()
        tuned = runner(
            staging_cfg,
            profile,
            workdir,
            progress=emit,
            prepare=tuned_prepare,
            on_rep=on_rep_hook,
        )
        timings["measurement_seconds"] = round(time.monotonic() - t0, 3)
        timings["measured_repetitions"] = len(tuned.per_run_tps)
        if on_rep_hook is not None and hook_fired:
            stopped_early = True
            emit(
                f"tuned arm stopped early after {len(tuned.per_run_tps)}/"
                f"{profile.repetitions} reps (futility)"
            )
        artifacts["tuned"] = write_artifact(
            run_dir, f"{label}-tuned", tuned.model_dump()
        )

        # 7b. Write-path evidence: baseline window vs tuned window vs net effect.
        wal_after = _collect_pg_write_stats(staging_cfg)
        baseline_window = _snapshot_delta(wal_before, wal_baseline_after)
        tuned_window = _snapshot_delta(wal_tuned_before, wal_after)
        wal_evidence = {
            "snapshots": {
                "before": wal_before,
                "baseline_after": wal_baseline_after,
                "tuned_before": wal_tuned_before,
                "after": wal_after,
            },
            "baseline_delta": baseline_window,
            "tuned_delta": tuned_window,
            "delta": _subtract_deltas(tuned_window, baseline_window),
        }
        artifacts["wal_evidence"] = write_artifact(
            run_dir, "wal-evidence", wal_evidence
        )

        # 8. A/B/A reversal: restore the pre-plan baseline and measure it again so
        #    the candidate is bracketed by baselines on both sides. The pooled
        #    per-run TPS cancels a monotonic page-cache-warming trend to first
        #    order. Best effort: any failure keeps the single A1 baseline, but
        #    the degradation is recorded machine-readably (and in an artifact)
        #    instead of only in a reason string that can be overwritten later.
        pooled_baseline = baseline
        reversal: SysbenchMeasurement | None = None
        if shared_baseline is not None:
            baseline_reversal = {
                "measured": False,
                "reason": "shared baseline reused across candidates",
            }
        elif not reversal_enabled:
            baseline_reversal = {
                "measured": False,
                "reason": "baseline reversal disabled for this pass",
            }
        elif settings_before:
            try:
                reversal_restored = False
                if (
                    mutates_dataset
                    and snapshot is not None
                    and snapshot_key
                    and snapshot.get(snapshot_key)
                ):
                    reversal_restored = _restore_from_snapshot("baseline reversal")
                reversal = _measure_baseline_reversal(
                    run_dir=run_dir,
                    plan=plan,
                    mode=mode,
                    container=container,
                    staging_cfg=staging_cfg,
                    profile=profile,
                    workdir=workdir,
                    settings_before=settings_before,
                    runner=runner,
                    benchmark_kind=benchmark_kind,
                    prepare=(mutates_dataset and not reversal_restored),
                    emit=emit,
                    timings=timings,
                    artifacts=artifacts,
                )
                pooled_baseline = _pooled_baseline(baseline, reversal)
                baseline_reversal = {
                    "measured": True,
                    "reason": "",
                    "artifact": artifacts.get("baseline_reversal", ""),
                }
                emit("baseline reversal measured; using pooled A1+A2 baseline")
            except Exception as e:
                baseline_reversal = {"measured": False, "reason": str(e)}
                reasons.append(
                    "baseline reversal not measured "
                    f"(falling back to single cold baseline): {e}"
                )
                emit(f"baseline reversal skipped: {e}")
        else:
            baseline_reversal = {
                "measured": False,
                "reason": "no pre-plan settings snapshot",
            }
            reasons.append(
                "baseline reversal not measured (no pre-plan settings snapshot); "
                "falling back to single cold baseline"
            )
        artifacts["baseline_reversal_status"] = write_artifact(
            run_dir, "baseline-reversal-status", baseline_reversal
        )

        # 8b. Surface the per-arm ignored-error counts. Ignored errors are
        #     recoverable DB errors (e.g. deadlocks) that sysbench swallows; a
        #     materially different count is recorded as a warning so the
        #     operator can see the arms may have done different work, but it
        #     never vetoes a win on its own: apply whenever tuned is not worse.
        ignored_errors = {
            "baseline_first": baseline.ignored_errors,
            "baseline_reversal": (
                reversal.ignored_errors if reversal is not None else None
            ),
            "tuned": tuned.ignored_errors,
            "pooled_baseline": pooled_baseline.ignored_errors,
        }
        ignored_error_mismatch, ignored_error_reason = _ignored_error_mismatch(
            baseline=baseline.ignored_errors,
            reversal=reversal.ignored_errors if reversal is not None else None,
            tuned=tuned.ignored_errors,
        )
        ignored_errors["mismatch"] = ignored_error_mismatch
        ignored_errors["reason"] = ignored_error_reason
        artifacts["ignored_errors"] = write_artifact(
            run_dir, "ignored-errors", ignored_errors
        )
        if ignored_error_mismatch:
            reasons.append(f"warning: {ignored_error_reason}")

        # 8c. Error-rate parity veto. Absolute ignored-error counts punish
        #     higher throughput (more total transactions at a parity error
        #     rate), so compare RATES: pooled-baseline rate vs tuned rate. A
        #     >50% rate excess with a material absolute gap means the arms did
        #     different work and the win cannot stand. The absolute gap is
        #     measured like-for-like against the single A1/shared baseline arm
        #     (the pooled arm covers ~2x the transactions when a reversal ran,
        #     so a pooled absolute comparison would never fire).
        error_rate_veto = False
        try:
            base_rate = _error_rate(
                pooled_baseline.ignored_errors, pooled_baseline
            )
            tuned_rate = _error_rate(tuned.ignored_errors, tuned)
            try:
                tuned_n = int(tuned.ignored_errors)
            except (TypeError, ValueError):
                tuned_n = 0
            try:
                base_n = int(baseline.ignored_errors)
            except (TypeError, ValueError):
                base_n = 0
            if tuned_n >= 5 or base_n >= 5:
                if base_rate <= 0.0:
                    if tuned_n >= 10:
                        error_rate_veto = True
                elif (
                    tuned_rate > base_rate * 1.5
                    and (tuned_n - base_n) >= 5
                ):
                    error_rate_veto = True
            if error_rate_veto:
                reasons.append(
                    f"tuned error rate {tuned_rate:.3%} exceeds baseline "
                    f"{base_rate:.3%} by >50% (tuned={tuned_n} errs, "
                    f"baseline={base_n} errs); arms not like-for-like"
                )
        except Exception:
            error_rate_veto = False

        # 9. Paired comparison against the pooled baseline.
        paired = PairedResult.evaluate(pooled_baseline, tuned, profile)

        # 10. Classify the overall status and collect human-readable reasons.
        for result in failed_applications:
            reasons.append(
                f"knob application failed for '{result.get('knob')}': {result.get('error')}"
            )
        for entry in verified_knobs:
            if entry.get("status") != "VERIFIED":
                reasons.append(
                    f"knob verification {entry.get('status')} for "
                    f"'{entry.get('knob')}': expected {entry.get('expected_value')}, "
                    f"actual {entry.get('actual_value')}"
                )
        if verification.get("error"):
            reasons.append(f"verification error: {verification['error']}")
        reasons.extend(paired.reasons)

        if failed_applications or restart_failed:
            status = TuningStatus.FAIL
        elif error_rate_veto:
            status = TuningStatus.FAIL
        elif paired.status == TuningStatus.PASS and all_verified:
            status = TuningStatus.PASS
        elif paired.status == TuningStatus.INCONCLUSIVE:
            status = TuningStatus.INCONCLUSIVE
        else:
            status = TuningStatus.FAIL

        # 11. Build and persist the attestation.
        artifacts["timings"] = timings
        attestation = _build_attestation(
            run_id=run_id,
            db_type=db_type,
            database=database,
            staging_cfg=staging_cfg,
            budget=budget,
            profile=profile,
            plan=plan,
            settings_before=settings_before,
            settings_after=settings_after,
            verified_knobs=verified_knobs,
            artifacts=artifacts,
            status=status,
            attempt=attempt,
        )
        write_artifact(
            run_dir, f"validation-attempt-{attempt}", attestation.model_dump()
        )

        emit(f"verdict: {status.value}" + (f" — {'; '.join(reasons)}" if reasons else ""))
        return _finalize(
            status,
            attestation,
            paired,
            reasons,
            staging_cfg,
            artifacts,
            wal_evidence,
            baseline_reversal,
            ignored_errors,
            stopped_early,
        )
    except Exception as e:
        status = TuningStatus.FAIL
        reasons.append(f"validation error: {e}")
        attestation = None
        if staging_cfg is not None:
            try:
                attestation = _build_attestation(
                    run_id=run_id,
                    db_type=db_type,
                    database=database,
                    staging_cfg=staging_cfg,
                    budget=budget,
                    profile=profile,
                    plan=plan,
                    settings_before=settings_before,
                    settings_after=settings_after,
                    verified_knobs=verification.get("knobs", []),
                    artifacts=artifacts,
                    status=status,
                    attempt=attempt,
                )
                write_artifact(
                    run_dir, f"validation-attempt-{attempt}", attestation.model_dump()
                )
            except Exception:
                attestation = None
        emit(f"verdict: {status.value}" + (f" — {'; '.join(reasons)}" if reasons else ""))
        return _finalize(
            status,
            attestation,
            paired,
            reasons,
            staging_cfg,
            artifacts,
            wal_evidence,
            baseline_reversal,
            ignored_errors,
        )
    finally:
        # 12. Always tear down the staging container (best effort).
        if container:
            emit("tearing down staging container...")
            try:
                stop_staging_db(container)
            except Exception:
                pass
