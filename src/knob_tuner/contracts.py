"""Pydantic contracts and enums shared across the knob_tuner pipeline."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from statistics import median
from typing import Any

from pydantic import BaseModel, Field, field_validator


class TuningStatus(str, Enum):
    """Overall outcome of a tuning attempt."""

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class KnobScope(str, Enum):
    """PostgreSQL ``pg_settings.context`` categories for a knob."""

    SIGHUP = "sighup"
    USER = "user"
    POSTMASTER = "postmaster"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class ApplyMode(str, Enum):
    """How a knob value is applied to a running database."""

    NONE = "none"
    DYNAMIC = "dynamic"
    PERSIST_STATIC = "persist-static"


class ResourceBudget(BaseModel):
    """Compute resources reserved for a tuning run."""

    cpu_cores: int = Field(gt=0)
    memory_gb: float = Field(gt=0)

    @field_validator("cpu_cores", "memory_gb", mode="before")
    @classmethod
    def _reject_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("boolean values are not valid resource amounts")
        return value

    def to_docker_cpus(self) -> str:
        """Return the CPU limit formatted for Docker (e.g. ``"4"``)."""
        return str(self.cpu_cores)

    def to_docker_memory(self) -> str:
        """Return the memory limit formatted for Docker (e.g. ``"8g"``)."""
        if self.memory_gb == int(self.memory_gb):
            return f"{int(self.memory_gb)}g"
        return f"{self.memory_gb}g"

    def to_dict(self) -> dict[str, Any]:
        """Return the budget as a plain dictionary."""
        return self.model_dump()


class SysbenchProfile(BaseModel):
    """Deterministic parameters for a sysbench benchmark."""

    profile_type: str = "oltp_read_write"
    tables: int = Field(default=10, gt=0)
    rows_per_table: int = Field(default=10000, gt=0)
    threads: int = Field(default=4, gt=0)
    warmup_seconds: int = Field(default=10, ge=0)
    measurement_seconds: int = Field(default=30, gt=0)
    repetitions: int = Field(default=3, ge=1)
    seed: int = 42
    throughput_threshold_pct: float = Field(default=0.0, ge=0)
    latency_threshold_pct: float = Field(default=5.0, ge=0)

    def profile_hash(self) -> str:
        """Return a stable sha256 hash of the profile contents."""
        canonical = json.dumps(
            self.model_dump(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class KnobSpec(BaseModel):
    """A single proposed knob assignment."""

    name: str
    value: Any
    scope: KnobScope = KnobScope.UNKNOWN
    restart_required: bool = False
    reasoning: str = ""


class KnobPlan(BaseModel):
    """An ordered collection of proposed knob assignments."""

    knobs: list[KnobSpec] = Field(default_factory=list)

    def plan_hash(self) -> str:
        """Return a stable sha256 hash over the name/value/scope of all knobs."""
        entries = [
            {"name": knob.name, "value": str(knob.value), "scope": knob.scope.value}
            for knob in self.knobs
        ]
        entries.sort(key=lambda entry: entry["name"])
        canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SysbenchMeasurement(BaseModel):
    """Result of running a sysbench benchmark, possibly repeated."""

    status: str = "error"
    tps: float = 0.0
    qps: float = 0.0
    latency_avg_ms: float = 0.0
    latency_p95_ms: float = 0.0
    ignored_errors: int = 0
    reconnects: int = 0
    threads: int = 0
    tables: int = 0
    rows_per_table: int = 0
    duration: int = 0
    seed: int = 0
    repetitions: int = 1
    per_run_tps: list[float] = Field(default_factory=list)
    log_file: str = ""
    error: str | None = None


class PairedResult(BaseModel):
    """Comparison of a baseline and tuned measurement against thresholds."""

    baseline: SysbenchMeasurement
    tuned: SysbenchMeasurement
    status: TuningStatus = TuningStatus.INCONCLUSIVE
    median_tps_baseline: float = 0.0
    median_tps_tuned: float = 0.0
    delta_pct: float = 0.0
    p95_delta_pct: float = 0.0
    reasons: list[str] = Field(default_factory=list)

    @classmethod
    def evaluate(
        cls,
        baseline: SysbenchMeasurement,
        tuned: SysbenchMeasurement,
        profile: SysbenchProfile,
    ) -> PairedResult:
        """Evaluate a baseline/tuned pair and classify it as PASS/FAIL/INCONCLUSIVE.

        Evidence must be complete before a run can PASS; any missing or noisy
        signal yields INCONCLUSIVE.
        """
        result = cls(baseline=baseline, tuned=tuned)
        reasons: list[str] = []

        median_baseline = (
            float(median(baseline.per_run_tps)) if baseline.per_run_tps else baseline.tps
        )
        median_tuned = (
            float(median(tuned.per_run_tps)) if tuned.per_run_tps else tuned.tps
        )
        result.median_tps_baseline = median_baseline
        result.median_tps_tuned = median_tuned
        if median_baseline > 0:
            result.delta_pct = (
                (median_tuned - median_baseline) / median_baseline * 100.0
            )
        if baseline.latency_p95_ms > 0:
            result.p95_delta_pct = (
                (tuned.latency_p95_ms - baseline.latency_p95_ms)
                / baseline.latency_p95_ms
                * 100.0
            )

        inconclusive = False
        if baseline.status != "ok":
            inconclusive = True
            reasons.append(
                f"baseline measurement status is '{baseline.status}', expected 'ok'"
            )
        if tuned.status != "ok":
            inconclusive = True
            reasons.append(
                f"tuned measurement status is '{tuned.status}', expected 'ok'"
            )
        if baseline.tps <= 0 or median_baseline <= 0:
            inconclusive = True
            reasons.append("baseline measured zero TPS")
        if tuned.tps <= 0 or median_tuned <= 0:
            inconclusive = True
            reasons.append("tuned measured zero TPS")
        if len(baseline.per_run_tps) < profile.repetitions:
            inconclusive = True
            reasons.append(
                f"baseline has {len(baseline.per_run_tps)} runs but "
                f"{profile.repetitions} repetitions are required"
            )
        if len(tuned.per_run_tps) < profile.repetitions:
            inconclusive = True
            reasons.append(
                f"tuned has {len(tuned.per_run_tps)} runs but "
                f"{profile.repetitions} repetitions are required"
            )
        # Ignored errors (recoverable DB-level errors) are recorded as warnings but
        # do not invalidate evidence; reconnects remain fatal.
        if baseline.reconnects > 0:
            inconclusive = True
            reasons.append(
                f"baseline recorded reconnects (reconnects={baseline.reconnects})"
            )
        if tuned.reconnects > 0:
            inconclusive = True
            reasons.append(f"tuned recorded reconnects (reconnects={tuned.reconnects})")
        if baseline.ignored_errors > 0:
            reasons.append(
                f"warning: baseline ignored_errors={baseline.ignored_errors}"
            )
        if tuned.ignored_errors > 0:
            reasons.append(f"warning: tuned ignored_errors={tuned.ignored_errors}")

        if inconclusive:
            result.status = TuningStatus.INCONCLUSIVE
            result.reasons = reasons
            return result

        failed = False
        throughput_floor = median_baseline * (
            1.0 - profile.throughput_threshold_pct / 100.0
        )
        if median_tuned < throughput_floor:
            failed = True
            reasons.append(
                f"median TPS dropped {abs(result.delta_pct):.2f}% "
                f"(allowed drop {profile.throughput_threshold_pct:.2f}%)"
            )
        if baseline.latency_p95_ms > 0:
            latency_ceiling = baseline.latency_p95_ms * (
                1.0 + profile.latency_threshold_pct / 100.0
            )
            if tuned.latency_p95_ms > latency_ceiling:
                failed = True
                reasons.append(
                    f"p95 latency increased {result.p95_delta_pct:.2f}% "
                    f"(allowed increase {profile.latency_threshold_pct:.2f}%)"
                )

        result.status = TuningStatus.FAIL if failed else TuningStatus.PASS
        result.reasons = reasons
        return result


class ValidationAttestation(BaseModel):
    """Evidence collected while validating a tuning attempt."""

    run_id: str
    database_identity: str
    db_engine: str
    resource_budget: ResourceBudget
    profile_hash: str
    seed: int
    plan_hash: str
    settings_before: dict[str, Any] = Field(default_factory=dict)
    settings_after: dict[str, Any] = Field(default_factory=dict)
    verified_knobs: list[dict[str, Any]] = Field(default_factory=list)
    benchmark_artifacts: dict[str, str] = Field(default_factory=dict)
    status: TuningStatus = TuningStatus.INCONCLUSIVE
    attempts: int = 0


class RunManifest(BaseModel):
    """Top-level manifest describing a single tuning run."""

    run_id: str
    timestamp: str
    status: TuningStatus = TuningStatus.INCONCLUSIVE
    resource_budget: dict[str, Any] = Field(default_factory=dict)
    db_engine: str = ""
    db_version: str = ""
    db_image: str = ""
    application_target: str = ""
    application_code_hash: str = ""
    knob_plan_hash: str = ""
    sysbench_profile_hash: str = ""
    seed: int = 0
    client_threads: int = 0
    attempt_count: int = 0
    applied_knobs: list[dict[str, Any]] = Field(default_factory=list)
    verified_knobs: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    final_status: str = "INCONCLUSIVE"
