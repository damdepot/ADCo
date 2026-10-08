"""Typed stage schemas for the knob-tuner redesign (Wave 1).

Schemas only — no wiring changes. These models type the boundaries between
pipeline stages; ``workflow.py``, agents, and tools are untouched.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from src.knob_tuner.contracts import VALID_EXPERIMENT_PHASES


class ProposedLevel(BaseModel):
    """One knob setting within a proposed single experiment.

    Relocated (not duplicated) from the retired
    ``src.knob_tuner.sub_agents.knob_recommender.models`` package; the
    retired ``KnobRecommendation``/``ExperimentProposal`` types were
    superseded by ``CandidateProposal`` and were not carried over.
    """

    knob: str = Field(description="Name of the database configuration knob/parameter")
    value: Any = Field(description="Proposed level value for this experiment")
    reasoning: str = Field(default="", description="DBA rationale for this level")

# Phase 4.1: canonical phases live in contracts.VALID_EXPERIMENT_PHASES.
_VALID_EXPERIMENT_PHASES = VALID_EXPERIMENT_PHASES

_VALID_TERMINAL_DECISIONS = (
    "apply_winner",
    "keep_best",
    "inconclusive",
    "fail",
)

_VALID_PREFLIGHT_ROUTES = (
    "auto",
    "maintenance_assisted",
    "blocked",
)


class CandidateProposal(BaseModel):
    """One proposed experiment: name/phase/levels required."""

    name: str = Field(description="Unique experiment name")
    phase: str = Field(
        description=(
            "MUST be exactly one of ('screen', 'interaction', 'refinement'). "
            "Never a knob name — knob names go only in levels[].knob."
        )
    )
    levels: list[ProposedLevel] = Field(
        min_length=1, description="Knob-level assignments (at least one required)"
    )
    rationale: str = Field(default="", description="DBA rationale for this experiment")
    objective: str = Field(default="", description="Tuning objective for this experiment")
    phase_raw: str = Field(
        default="", description="Raw phase value as received before repair"
    )
    repaired: bool = Field(
        default=False, description="True when phase was repaired to a valid value"
    )

    @model_validator(mode="before")
    @classmethod
    def _repair_phase(cls, data: Any) -> Any:
        # Repair (never raise): the LLM sometimes puts a knob name in the
        # phase field; ADK output_schema validation would otherwise escape
        # as ValidationError and kill the whole Workflow. Valid phases pass
        # through normalized; anything else repairs to "screen" with the raw
        # value preserved in phase_raw.
        if isinstance(data, dict):
            raw = data.get("phase", "")
            normalized = str(raw or "").strip().lower()
            if normalized in _VALID_EXPERIMENT_PHASES:
                patched = dict(data)
                patched["phase"] = normalized
                patched.setdefault("repaired", False)
                patched.setdefault("phase_raw", "")
                return patched
            patched = dict(data)
            try:
                raw_str = str(raw) if raw is not None else ""
            except Exception:
                raw_str = ""
            patched["phase"] = "screen"
            patched["phase_raw"] = raw_str
            patched["repaired"] = True
            return patched
        return data


class CorrectionType(str, Enum):
    """Kind of correction a diagnosis stage may request."""

    SHRINK_SET = "shrink_set"
    CHANGE_PHASE = "change_phase"
    DROP_KNOB = "drop_knob"
    ADJUST_VALUE = "adjust_value"
    RETRY_SAME = "retry_same"
    STOP = "stop"


class DiagnosisOutput(BaseModel):
    """Diagnosis verdict for a rejected/failed proposal."""

    correction: CorrectionType = Field(description="Requested correction kind")
    targets: list[str] = Field(
        default_factory=list, description="Knob names the correction applies to"
    )
    rationale: str = Field(default="", description="Rationale for the correction")
    confidence: float = Field(
        description=(
            "Agent's confidence (0..1) that the recommended action "
            "(the correction enum) is correct"
        )
    )
    stop_reason: str = Field(
        default="futility",
        description="Why a stop is requested: 'winner' or 'futility'",
    )

    @field_validator("confidence", mode="before")
    @classmethod
    def _check_confidence(cls, value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError(  # noqa: TRY004 - pydantic only wraps ValueError here
                f"confidence must be a number in [0, 1], got {value!r}"
            )
        try:
            conf = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"confidence must be a number in [0, 1], got {value!r}"
            )
        if not (0.0 <= conf <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {value!r}")
        return conf

    @field_validator("stop_reason", mode="before")
    @classmethod
    def _check_stop_reason(cls, value: Any) -> str:
        normalized = str(value or "futility").strip().lower()
        if normalized == "":
            return "futility"
        if normalized not in ("winner", "futility"):
            raise ValueError(
                f"stop_reason must be 'winner' or 'futility', got {value!r}"
            )
        return normalized


class CompiledPlan(BaseModel):
    """A validated proposal compiled to a KnobPlan dump."""

    plan: dict[str, Any] = Field(description="KnobPlan model dump")
    exp_name: str = Field(description="Source experiment name")
    phase: str = Field(description="Experiment phase")
    valid_knobs: list[str] = Field(
        default_factory=list, description="Knob names that passed validation"
    )


class CompileRejection(BaseModel):
    """Rejection of a proposal that could not be compiled."""

    reason: str = Field(description="Short rejection reason")
    errors: list[str] = Field(default_factory=list, description="Detailed errors")
    design_name: str = Field(default="", description="Name of the rejected design")


class KnobBelief(BaseModel):
    """Per-knob running belief: best observed delta and exposure count."""

    best_delta_pct: float = 0.0
    n_seen: int = 0
    last_phase: str = ""
    cleared: bool = False


def render_belief_table(beliefs: dict[str, KnobBelief]) -> str:
    """Render beliefs as one markdown-table line per knob, best first.

    Sorted by ``best_delta_pct`` descending, capped at 20 knob rows.
    Never throws: bad input yields a short placeholder string.
    """
    try:
        items: list[tuple[str, float, int, str, bool]] = []
        for name, belief in (beliefs or {}).items():
            if isinstance(belief, KnobBelief):
                best, seen, phase, cleared = (
                    belief.best_delta_pct,
                    belief.n_seen,
                    belief.last_phase,
                    belief.cleared,
                )
            elif isinstance(belief, dict):
                try:
                    best = float(belief.get("best_delta_pct", 0.0) or 0.0)
                except (TypeError, ValueError):
                    best = 0.0
                try:
                    seen = int(belief.get("n_seen", 0) or 0)
                except (TypeError, ValueError):
                    seen = 0
                phase = str(belief.get("last_phase", "") or "")
                cleared = bool(belief.get("cleared", False))
            else:
                continue
            items.append((str(name), best, seen, phase, cleared))
        if not items:
            return "No knob beliefs yet."
        items.sort(key=lambda item: item[1], reverse=True)
        lines = [
            "| knob | best_delta_pct | n_seen | last_phase | cleared |",
            "| --- | --- | --- | --- | --- |",
        ]
        for name, best, seen, phase, cleared in items[:20]:
            lines.append(
                f"| {name} | {best:+.2f}% | {seen} | {phase} | "
                f"{'yes' if cleared else 'no'} |"
            )
        if len(items) > 20:
            lines.append(f"... +{len(items) - 20} more omitted (cap 20)")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - render must never throw
        return "No knob beliefs yet."


class ScreenVerdict(BaseModel):
    """Outcome of screening a compiled plan against the shared baseline."""

    status: str = Field(description="Validation status (e.g. PASS/FAIL/INCONCLUSIVE)")
    mean_delta_pct: float = Field(default=0.0)
    lcb_pct: float = Field(default=0.0)
    ucb_pct: float = Field(default=0.0)
    confirmed: bool = Field(default=False)
    improvement_confident: bool = Field(default=False)
    stopped_early: bool = Field(default=False)
    paired: dict[str, Any] | None = Field(
        default_factory=dict,
        description="Paired per-run samples (None when no measurement exists)",
    )
    reasons: list[str] = Field(default_factory=list)


class TerminalDecision(BaseModel):
    """Final loop decision once the experiment budget is exhausted."""

    decision: str = Field(description="One of apply_winner/keep_best/inconclusive/fail")
    winner_plan: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)

    @field_validator("decision", mode="before")
    @classmethod
    def _check_decision(cls, value: Any) -> str:
        if value not in _VALID_TERMINAL_DECISIONS:
            raise ValueError(
                f"decision must be one of {_VALID_TERMINAL_DECISIONS}, got {value!r}"
            )
        return str(value)


class PreflightVerdict(BaseModel):
    """Pre-apply routing verdict (safe-auto vs maintenance-assisted vs blocked)."""

    route: str = Field(description="One of auto/maintenance_assisted/blocked")
    reason: str = Field(default="", description="Reason for the routing decision")

    @field_validator("route", mode="before")
    @classmethod
    def _check_route(cls, value: Any) -> str:
        if value not in _VALID_PREFLIGHT_ROUTES:
            raise ValueError(
                f"route must be one of {_VALID_PREFLIGHT_ROUTES}, got {value!r}"
            )
        return str(value)


_WORKLOAD_FIELDS = (
    "query_types",
    "orm_detected",
    "transaction_pattern",
    "estimated_read_write_ratio",
    "notable_patterns",
)


def normalize_workload_profile(
    workload_info: dict | None, workload_hint: str
) -> dict[str, Any]:
    """Merge WorkloadPattern fields + hint string into one workload_profile dict.

    Never throws: bad input yields ``{}`` (plus the hint when present).
    """
    profile: dict[str, Any] = {}
    if isinstance(workload_info, dict):
        nested = workload_info.get("workload")
        if isinstance(nested, dict):
            for key in _WORKLOAD_FIELDS:
                if key in nested:
                    profile[key] = nested[key]
            for key, val in workload_info.items():
                if key != "workload" and key not in profile:
                    profile[key] = val
        else:
            for key in _WORKLOAD_FIELDS:
                if key in workload_info:
                    profile[key] = workload_info[key]
            for key, val in workload_info.items():
                if key not in profile:
                    profile[key] = val

    if isinstance(workload_hint, str) and workload_hint.strip():
        profile["workload_hint"] = workload_hint.strip()
    return profile
