"""Pydantic contracts and enums shared across the knob_tuner pipeline."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from statistics import median
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TuningStatus(str, Enum):
    """Overall outcome of a tuning attempt."""

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# Tuning defaults — single home for pipeline-wide constants (Phase 4.1).
#
# Canonical source for the experiment phases, the loop caps, and the win
# gate. Every other site (stages/models.py, stages/nodes.py,
# tools/experiments.py, workflow.py, main.py, prompts) imports from here or
# carries only a prose copy with a comment pointing back here.
# ---------------------------------------------------------------------------

#: Valid experiment phases for CandidateProposal/ExperimentArm/CompiledPlan.
VALID_EXPERIMENT_PHASES: tuple[str, str, str] = (
    "screen",
    "interaction",
    "refinement",
)

#: Default maximum screening attempts before the loop stops.
DEFAULT_MAX_ATTEMPTS: int = 30

#: Default number of LCB-clearing winners a campaign must collect before the
#: loop may stop on a winner (compounding DOE campaign). A winner stop is
#: premature while fewer than this many candidates have cleared the win gate.
DEFAULT_SUCCESS_CANDIDATES: int = 3

#: Default maximum certified winners before the loop stops.
DEFAULT_MAX_WINNERS: int = 3

#: Default cap on distinct knobs per experiment proposal.
DEFAULT_MAX_SET_KNOBS: int = 20

#: Default win-gate: LCB on throughput must exceed this pct to confirm.
DEFAULT_MIN_IMPROVEMENT_PCT: float = 5.0

#: Promotion bar: LCB on throughput must exceed this pct to certify. Any
#: confident win certifies, while min_improvement_pct stays the
#: ranking/display gate.
DEFAULT_CERTIFY_LCB_PCT: float = 1.0

#: Default measurement reps per arm in single-fidelity mode.
DEFAULT_MEASURE_REPS: int = 10

#: Default measured seconds per repetition.
DEFAULT_MEASURE_SECONDS: int = 10

#: Default warmup seconds per measurement arm: ~10s of load covers the
#: observed post-restore cold window (rep-1 dips resolve within the first
#: measured rep), with margin kept small since warmup runs per arm.
DEFAULT_MEASURE_WARMUP_SECONDS: int = 8

#: Default minimum reps before an arm may stop early for futility.
DEFAULT_EARLY_STOP_MIN_REPS: int = 4

#: Canonical session-state key for the database name (Phase 4.3, R3 done).
#: Legacy ``db_name``/``dbname`` mirrors deleted; readers use ``database``.
#: :func:`get_database_name` stays tolerant (harmless compat read).
DATABASE_STATE_KEY: str = "database"

#: Canonical session-state key for the loop counter (Phase 4.3, R3 done).
#: Legacy ``attempt`` mirror deleted; readers use ``validation_attempt_count``.
ATTEMPT_STATE_KEY: str = "validation_attempt_count"

#: Canonical session-state key for the winner-quota target.
SUCCESS_CANDIDATES_STATE_KEY: str = "success_candidates"

#: Canonical session-state key for the shared baseline (Phase 4.3, R3 done).
#: Legacy ``baseline`` mirror deleted; readers use ``shared_baseline``.
SHARED_BASELINE_STATE_KEY: str = "shared_baseline"

#: Canonical session-state key for the DB config path (Phase 4.3, R3 done).
#: Legacy ``config_path`` mirror deleted; readers use ``db_config_path``.
DB_CONFIG_PATH_STATE_KEY: str = "db_config_path"

#: Canonical session-state keys for the single-fidelity timing family.
MEASURE_REPS_STATE_KEY: str = "measure_reps"
MEASURE_SECONDS_STATE_KEY: str = "measure_seconds"
MEASURE_WARMUP_SECONDS_STATE_KEY: str = "measure_warmup_seconds"
EARLY_STOP_MIN_REPS_STATE_KEY: str = "early_stop_min_reps"


def get_database_name(state: Any) -> str:
    """Return the canonical database name (tolerant legacy read, harmless).

    R3 keeps the ``db_name``/``dbname`` fallback: old persisted states and
    embedded callers may still carry the deleted mirrors, and a read-only
    fallback can never diverge writes (writes are canonical-only).
    """
    try:
        getter = getattr(state, "get", None)
        if not callable(getter):
            return ""
        return str(
            getter("database") or getter("db_name") or getter("dbname") or ""
        ).strip()
    except Exception:
        return ""


def get_db_config_path(state: Any) -> str:
    """Return the canonical DB config path (R3: canonical-only)."""
    try:
        getter = getattr(state, "get", None)
        if not callable(getter):
            return ""
        value = getter("db_config_path") or ""
        return str(value).strip()
    except Exception:
        return ""


def get_validation_attempt(state: Any) -> int:
    """Return the canonical loop counter (R3: canonical-only)."""
    try:
        getter = getattr(state, "get", None)
        if not callable(getter):
            return 0
        raw = getter("validation_attempt_count", 0)
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def get_max_attempts(state: Any) -> int:
    """Return the loop cap (default :data:`DEFAULT_MAX_ATTEMPTS`)."""
    try:
        getter = getattr(state, "get", None)
        raw = getter("max_attempts", DEFAULT_MAX_ATTEMPTS) if callable(getter) else DEFAULT_MAX_ATTEMPTS
        return max(1, int(raw or DEFAULT_MAX_ATTEMPTS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ATTEMPTS


def _resolve_quota_target(state: Any) -> int:
    """Return the unified winner-quota target (never throws).

    ``success_candidates`` is canonical; ``max_winners`` is a working alias
    onto the same quota (both CLI flags feed one state value). The effective
    target is the max over the keys actually present, so either flag can set
    the bar in hand-built states; absent/unparseable keys fall back to
    :data:`DEFAULT_SUCCESS_CANDIDATES`. Floored at 1 so a campaign always
    requires at least one certified winner before a winner stop is complete.
    """
    try:
        getter = getattr(state, "get", None)
        if not callable(getter):
            return DEFAULT_SUCCESS_CANDIDATES
        values: list[int] = []
        for key in (SUCCESS_CANDIDATES_STATE_KEY, "max_winners"):
            try:
                raw = getter(key, None)
            except Exception:
                continue
            if raw is None:
                continue
            try:
                text = str(raw).strip()
            except Exception:
                continue
            if not text:
                continue
            try:
                parsed = int(float(text))
            except (TypeError, ValueError):
                continue
            if parsed == 0:
                # A zero quota is meaningless (same as unset): fall back to
                # the default rather than flooring to 1.
                continue
            values.append(parsed)
        if not values:
            return DEFAULT_SUCCESS_CANDIDATES
        return max(1, max(values))
    except Exception:
        return DEFAULT_SUCCESS_CANDIDATES


def get_success_candidates(state: Any) -> int:
    """Return the winner-quota target (default :data:`DEFAULT_SUCCESS_CANDIDATES`).

    Canonical reader for the unified quota (see :func:`_resolve_quota_target`).
    """
    return _resolve_quota_target(state)


def get_max_winners(state: Any) -> int:
    """Return the winner quota (alias onto :func:`get_success_candidates`).

    ``max_winners`` maps onto the same quota as ``success_candidates``
    (single check, single target); both readers agree by construction.
    """
    return _resolve_quota_target(state)


def get_min_improvement_pct(state: Any) -> float:
    """Return the win-gate pct (explicit 0.0 honored; missing → default).

    Uses a plain ``.get`` default WITHOUT ``or`` so a configured ``0.0``
    is honored while a missing/``None`` entry falls back to
    :data:`DEFAULT_MIN_IMPROVEMENT_PCT`.
    """
    try:
        getter = getattr(state, "get", None)
        raw = getter("min_improvement_pct", DEFAULT_MIN_IMPROVEMENT_PCT) if callable(getter) else DEFAULT_MIN_IMPROVEMENT_PCT
        if raw is None:
            return DEFAULT_MIN_IMPROVEMENT_PCT
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_IMPROVEMENT_PCT


def get_certify_lcb_pct(state: Any) -> float:
    """Return the promotion-bar pct (explicit 0.0 honored; missing → default).

    Uses a plain ``.get`` default WITHOUT ``or`` so a configured ``0.0``
    is honored while a missing/``None`` entry falls back to
    :data:`DEFAULT_CERTIFY_LCB_PCT`.
    """
    try:
        getter = getattr(state, "get", None)
        raw = getter("certify_lcb_pct", DEFAULT_CERTIFY_LCB_PCT) if callable(getter) else DEFAULT_CERTIFY_LCB_PCT
        if raw is None:
            return DEFAULT_CERTIFY_LCB_PCT
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CERTIFY_LCB_PCT


def get_max_set_knobs(state: Any) -> int:
    """Return the per-experiment knob cap (default :data:`DEFAULT_MAX_SET_KNOBS`)."""
    try:
        getter = getattr(state, "get", None)
        raw = getter("max_set_knobs", DEFAULT_MAX_SET_KNOBS) if callable(getter) else DEFAULT_MAX_SET_KNOBS
        return max(1, int(raw or DEFAULT_MAX_SET_KNOBS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_SET_KNOBS


def _read_clamped_int(state: Any, key: str, default: int, floor: int) -> int:
    """Read *key* from *state* and apply *floor*, falling back to *default*.

    Single source for the timing family's default + clamp: a missing/``None``
    value yields *default*; any other value is coerced to ``int`` and floored
    at *floor* (bad types yield *default*).
    """
    try:
        getter = getattr(state, "get", None)
        raw = getter(key, default) if callable(getter) else default
        if raw is None:
            return default
        return max(floor, int(raw))
    except (TypeError, ValueError):
        return default


def get_measure_reps(state: Any) -> int:
    """Return measurement reps per arm (default :data:`DEFAULT_MEASURE_REPS`, min 2)."""
    return _read_clamped_int(state, MEASURE_REPS_STATE_KEY, DEFAULT_MEASURE_REPS, 2)


def get_measure_seconds(state: Any) -> int:
    """Return measured seconds per rep (default :data:`DEFAULT_MEASURE_SECONDS`, min 1)."""
    return _read_clamped_int(state, MEASURE_SECONDS_STATE_KEY, DEFAULT_MEASURE_SECONDS, 1)


def get_measure_warmup_seconds(state: Any) -> int:
    """Return warmup seconds per arm (default :data:`DEFAULT_MEASURE_WARMUP_SECONDS`, min 0)."""
    return _read_clamped_int(
        state, MEASURE_WARMUP_SECONDS_STATE_KEY, DEFAULT_MEASURE_WARMUP_SECONDS, 0
    )


def get_early_stop_min_reps(state: Any) -> int:
    """Return futility early-stop floor (default :data:`DEFAULT_EARLY_STOP_MIN_REPS`, min 2)."""
    return _read_clamped_int(
        state, EARLY_STOP_MIN_REPS_STATE_KEY, DEFAULT_EARLY_STOP_MIN_REPS, 2
    )


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
    LIVE = "live"
    MANUAL = "manual"


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

    model_config = ConfigDict(extra="forbid")

    profile_type: str = "oltp_read_write"
    tables: int = Field(default=10, gt=0)
    rows_per_table: int = Field(default=10000, gt=0)
    threads: int = Field(default=4, gt=0)
    warmup_seconds: int = Field(default=10, ge=0)
    measurement_seconds: int = Field(default=30, gt=0)
    repetitions: int = Field(default=3, ge=1)
    seed: int = 42
    rand_type: str = "pareto"
    throughput_threshold_pct: float = Field(default=0.0, ge=0)
    latency_threshold_pct: float = Field(default=5.0, ge=0)
    min_improvement_pct: float = Field(
        default=5.0,
        ge=0,
        description=(
            "Minimum percentage the 95% lower confidence bound on throughput "
            "must exceed for a candidate to be considered a real improvement. "
            "Proxy overstates production ~1.6-2x on OLTP, so the gate requires "
            "5% proxy LCB."
        ),
    )

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
    prepare_seconds: float = 0.0
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
    pending_restart_knobs: list[dict[str, Any]] = Field(default_factory=list)
    validation_timings: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    final_status: str = "INCONCLUSIVE"
