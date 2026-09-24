"""Deterministic (non-LLM) validation orchestrator for knob tuning plans.

This module provisions an isolated staging database, measures a baseline,
applies the candidate plan, optionally restarts the staging container, verifies
the resulting settings, measures again, and produces a paired comparison plus a
:class:`ValidationAttestation`. It contains no LLM calls and never raises for
expected failure modes; every outcome is reported through a structured dict.
"""

from __future__ import annotations

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
from src.knob_tuner.tools.benchmark_tools import run_sysbench_measurement
from src.knob_tuner.tools.db_connector import DBConfig
from src.knob_tuner.tools.db_tools import (
    apply_knobs,
    snapshot_settings,
    verify_active_knobs,
)
from src.knob_tuner.tools.docker_tools import (
    get_container_host_port,
    restart_docker_db,
    start_staging_db,
    stop_staging_db,
)
from src.knob_tuner.tools.run_artifacts import write_artifact

_RESTART_MODES = (ApplyMode.PERSIST_STATIC,)


def _finalize(
    status: TuningStatus,
    attestation: ValidationAttestation | None,
    paired: PairedResult | None,
    reasons: list[str],
    staging_db_config: DBConfig | None,
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    """Build the JSON-serializable return payload (except the DBConfig)."""
    return {
        "status": status.value,
        "attestation": attestation.model_dump() if attestation is not None else None,
        "paired": paired.model_dump() if paired is not None else None,
        "reasons": reasons,
        "staging_db_config": staging_db_config,
        "artifacts": artifacts,
    }


def _coerce_apply_mode(value: Any) -> ApplyMode:
    if isinstance(value, ApplyMode):
        return value
    try:
        return ApplyMode(str(value).strip().lower())
    except (ValueError, AttributeError):
        return ApplyMode.DYNAMIC


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
        },
        status=status,
        attempts=attempt,
    )


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
    progress: Callable[[str], None] | None = None,
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

    Returns:
        A structured dict with ``status``, ``attestation``, ``paired``,
        ``reasons``, ``staging_db_config`` and ``artifacts``.
    """
    emit = progress or (lambda _message: None)

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

    mode = _coerce_apply_mode(apply_mode)

    reasons: list[str] = []
    artifacts: dict[str, Any] = {}
    container: str | None = None
    staging_cfg: DBConfig | None = None
    baseline: SysbenchMeasurement | None = None
    tuned: SysbenchMeasurement | None = None
    paired: PairedResult | None = None
    settings_before: dict[str, str] = {}
    settings_after: dict[str, str] = {}
    verification: dict[str, Any] = {"knobs": [], "all_verified": False, "status": "error"}
    applied_results: list[dict[str, Any]] = []
    failed_applications: list[dict[str, Any]] = []
    restart_failed = False
    status = TuningStatus.FAIL

    try:
        # 1. Provision the isolated staging container (also verifies resources).
        emit(
            f"provisioning staging {db_type} {db_version or 'default'} "
            f"({budget.cpu_cores} CPU / {budget.memory_gb} GB)..."
        )
        try:
            container, staging_cfg = start_staging_db(
                db_type=db_type,
                db_version=db_version,
                budget=budget,
                database=database,
            )
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

        names = [knob.name for knob in plan.knobs]

        # 2. Snapshot settings before any mutation.
        settings_before = snapshot_settings(staging_cfg, names)

        # 3. Baseline measurement.
        emit("running baseline measurement...")
        baseline = run_sysbench_measurement(
            staging_cfg, profile, workdir, progress=emit
        )
        artifacts["baseline"] = write_artifact(
            run_dir, "sysbench-baseline", baseline.model_dump()
        )

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
        applied_results = apply_knobs(raw_knobs, staging_cfg, dry_run=False, mode=mode)
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
            ok, message = restart_docker_db(container, db_type=db_type)
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

        # 7. Tuned measurement.
        emit("running tuned measurement...")
        tuned = run_sysbench_measurement(
            staging_cfg, profile, workdir, progress=emit
        )
        artifacts["tuned"] = write_artifact(
            run_dir, "sysbench-tuned", tuned.model_dump()
        )

        # 8. Paired comparison.
        paired = PairedResult.evaluate(baseline, tuned, profile)

        # 9. Classify the overall status and collect human-readable reasons.
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
        elif paired.status == TuningStatus.PASS and all_verified:
            status = TuningStatus.PASS
        elif paired.status == TuningStatus.INCONCLUSIVE:
            status = TuningStatus.INCONCLUSIVE
        else:
            status = TuningStatus.FAIL

        # 10. Build and persist the attestation.
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
            status, attestation, paired, reasons, staging_cfg, artifacts
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
        return _finalize(status, attestation, paired, reasons, staging_cfg, artifacts)
    finally:
        # 11. Always tear down the staging container (best effort).
        if container:
            emit("tearing down staging container...")
            try:
                stop_staging_db(container)
            except Exception:
                pass
