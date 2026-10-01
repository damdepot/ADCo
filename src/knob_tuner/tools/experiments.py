"""Deterministic experiment-protocol expansion and result helpers.

Pure core for LLM-designed tuning experiments: no LLM calls, no DB calls.
An LLM proposes an :class:`ExperimentProtocol`; this module validates it
against budget caps, expands each arm into a :class:`KnobPlan`, and later
summarizes arm results and picks a winner.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.knob_tuner.contracts import KnobPlan, KnobScope, KnobSpec
from src.knob_tuner.tools.knobs import build_plan
from src.knob_tuner.tools.stats import welch_delta

__all__ = [
    "MAX_INTERACTION_ARMS",
    "MAX_REFINEMENT_ARMS",
    "MAX_SCREEN_ARMS",
    "MAX_TOTAL_ARMS",
    "VALID_PHASES",
    "ExperimentArm",
    "ExperimentLevel",
    "ExperimentProtocol",
    "KnobPlan",
    "KnobScope",
    "KnobSpec",
    "expand_protocol",
    "format_protocol_feedback",
    "pick_winner",
    "run_experiment_arms",
    "summarize_arm_results",
]

MAX_SCREEN_ARMS = 6
MAX_INTERACTION_ARMS = 4
MAX_REFINEMENT_ARMS = 4
MAX_TOTAL_ARMS = 12

VALID_PHASES = ("screen", "interaction", "refinement")


class ExperimentLevel(BaseModel):
    """One knob setting within an experiment arm."""

    knob: str
    value: Any
    reasoning: str = ""


class ExperimentArm(BaseModel):
    """One arm of an experiment protocol (a full knob-level assignment)."""

    name: str
    phase: str
    levels: list[ExperimentLevel] = Field(min_length=1)
    rationale: str = ""

    @field_validator("phase", mode="before")
    @classmethod
    def _normalize_phase(cls, value: Any) -> str:
        text = value.strip().lower() if isinstance(value, str) else ""
        if text not in VALID_PHASES:
            raise ValueError(f"phase must be one of {VALID_PHASES}, got {value!r}")
        return text


class ExperimentProtocol(BaseModel):
    """LLM-designed experiment: a set of arms across phases."""

    objective: str = ""
    arms: list[ExperimentArm] = Field(min_length=1)
    summary: str = ""


def expand_protocol(
    protocol: ExperimentProtocol,
    context_map: dict[str, str],
    min_distinct_knobs: int = 20,
) -> tuple[list[tuple[KnobPlan, str, str]], list[str]]:
    """Expand each arm into a ``KnobPlan`` or reject the whole protocol.

    All rejections are returned (never raised); on any rejection the
    result is ``([], reasons)`` (all-or-nothing).
    """
    reasons: list[str] = []
    counts = dict.fromkeys(VALID_PHASES, 0)

    for arm in protocol.arms:
        phase = arm.phase.strip().lower() if isinstance(arm.phase, str) else ""
        if phase not in VALID_PHASES:
            reasons.append(f"arm {arm.name!r}: unknown phase {arm.phase!r}")
            continue
        counts[phase] += 1
        if not arm.levels:
            reasons.append(f"arm {arm.name!r}: empty levels")
            continue
        names = [level.knob for level in arm.levels]
        if len(set(names)) != len(names):
            reasons.append(f"arm {arm.name!r}: duplicate knob within arm")

    if len(protocol.arms) > MAX_TOTAL_ARMS:
        reasons.append(
            f"total arms {len(protocol.arms)} exceeds cap {MAX_TOTAL_ARMS}"
        )
    if counts["screen"] > MAX_SCREEN_ARMS:
        reasons.append(
            f"screen arms {counts['screen']} exceeds cap {MAX_SCREEN_ARMS}"
        )
    if counts["interaction"] > MAX_INTERACTION_ARMS:
        reasons.append(
            f"interaction arms {counts['interaction']} exceeds cap "
            f"{MAX_INTERACTION_ARMS}"
        )
    if counts["refinement"] > MAX_REFINEMENT_ARMS:
        reasons.append(
            f"refinement arms {counts['refinement']} exceeds cap "
            f"{MAX_REFINEMENT_ARMS}"
        )

    distinct = {level.knob for arm in protocol.arms for level in arm.levels}
    if len(distinct) < min_distinct_knobs:
        reasons.append(
            f"distinct knobs {len(distinct)} below floor {min_distinct_knobs}"
        )

    if reasons:
        return ([], reasons)

    plans: list[tuple[KnobPlan, str, str]] = []
    for arm in protocol.arms:
        raw = [
            {"name": level.knob, "value": level.value, "reasoning": level.reasoning}
            for level in arm.levels
        ]
        plans.append((build_plan(raw, context_map), arm.phase, arm.name))
    return (plans, [])


def _num(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def summarize_arm_results(rows: list[dict[str, Any]]) -> str:
    """Render arm result rows as a markdown table plus a best-arm note."""
    header = "| arm | phase | mean_delta_pct | lcb_pct | ucb_pct | status | reps |"
    lines = [header, "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(
            f"| {row.get('arm', '')} | {row.get('phase', '')} | "
            f"{_num(row, 'mean_delta_pct'):.2f} | {_num(row, 'lcb_pct'):.2f} | "
            f"{_num(row, 'ucb_pct'):.2f} | {row.get('status', '')} | "
            f"{row.get('reps', '')} |"
        )
    winner = pick_winner(rows)
    if winner is None:
        lines.append("Best arm: none (no PASS rows).")
    else:
        lines.append(
            f"Best arm: {winner.get('arm', '')} "
            f"({_num(winner, 'mean_delta_pct'):+.2f}% mean delta)."
        )
    return "\n".join(lines)


def pick_winner(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the PASS row with the highest mean, tie-broken by lcb."""
    passing = [row for row in rows if row.get("status") == "PASS"]
    if not passing:
        return None
    return max(passing, key=lambda row: (_num(row, "mean_delta_pct"), _num(row, "lcb_pct")))


def run_experiment_arms(
    *,
    arms: list[tuple[KnobPlan, str, str]],
    shared_baseline: Any,
    validate_fn: Callable[..., Any],
    run_profile: Any,
    attempt_base: int,
    early_stop_min_reps: int | None,
    progress: Callable[[str], None] | None = None,
    min_improvement_pct: float = 5.0,
) -> list[dict[str, Any]]:
    """Validate each experiment arm and score it with Welch stats.

    ``validate_fn`` is an injected callable (the workflow passes a closure
    over the real validator; tests pass a mock). Never raises for arm
    failures: each arm is isolated in try/except and yields an ERROR row.
    """

    def _emit(message: str) -> None:
        if progress is None:
            return
        with suppress(Exception):
            progress(message)

    def _as_list(values: Any) -> list[float]:
        if values is None:
            return []
        if isinstance(values, (list, tuple)):
            items = list(values)
        else:
            return []
        samples: list[float] = []
        for item in items:
            try:
                samples.append(float(item))
            except (TypeError, ValueError):
                continue
        return samples

    def _side_samples(paired: Any, key: str) -> list[float]:
        if paired is None:
            return []
        side: Any
        if isinstance(paired, dict):
            side = paired.get(key)
        else:
            side = getattr(paired, key, None)
        if side is None:
            return []
        raw: Any
        if isinstance(side, dict):
            raw = side.get("per_run_tps")
        else:
            raw = getattr(side, "per_run_tps", None)
        return _as_list(raw or [])

    def _status_of(result: Any) -> str:
        if isinstance(result, dict):
            return str(result.get("status", ""))
        return str(getattr(result, "status", ""))

    def _reasons_of(result: Any) -> list[str]:
        if isinstance(result, dict):
            reasons = result.get("reasons", [])
        else:
            reasons = getattr(result, "reasons", [])
        if not reasons:
            return []
        return [str(r) for r in list(reasons)]

    def _paired_of(result: Any) -> Any:
        if isinstance(result, dict):
            return result.get("paired")
        return getattr(result, "paired", None)

    def _stopped_of(result: Any) -> bool:
        if isinstance(result, dict):
            return bool(result.get("stopped_early", False))
        return bool(getattr(result, "stopped_early", False))

    rows: list[dict[str, Any]] = []
    for index, (plan, phase, arm_name) in enumerate(arms):
        attempt = attempt_base + index
        plan_hash = plan.plan_hash()
        _emit(f"validating arm {arm_name} [{phase}] (attempt {attempt})...")
        try:
            result = validate_fn(
                plan=plan,
                run_profile=run_profile,
                shared_baseline=shared_baseline,
                attempt=attempt,
                early_stop_min_reps=early_stop_min_reps,
            )
        except Exception as exc:  # noqa: BLE001 - per-arm isolation must not raise
            _emit(f"arm {arm_name} error: {exc}")
            rows.append(
                {
                    "arm": arm_name,
                    "phase": phase,
                    "plan_hash": plan_hash,
                    "status": "ERROR",
                    "mean_delta_pct": 0.0,
                    "lcb_pct": 0.0,
                    "ucb_pct": 0.0,
                    "df": 0.0,
                    "reps": 0,
                    "stopped_early": False,
                    "confirmed": False,
                    "improvement_confident": False,
                    "paired": None,
                    "reasons": [str(exc)],
                    "result": None,
                    "plan": plan,
                }
            )
            continue
        paired = _paired_of(result)
        baseline_tps = _side_samples(paired, "baseline")
        tuned_tps = _side_samples(paired, "tuned")
        stats = welch_delta(baseline_tps, tuned_tps)
        mean = float(stats.get("mean_delta_pct", 0.0))
        lcb = float(stats.get("lcb_pct", 0.0))
        ucb = float(stats.get("ucb_pct", 0.0))
        df = float(stats.get("df", 0.0))
        status = _status_of(result)
        healthy = status.upper() == "PASS"
        not_worse = df >= 1 and mean >= 0
        confirmed = bool(healthy and not_worse)
        _emit(
            f"arm {arm_name}: delta={mean:.2f}% status={status} "
            f"→ {'CONFIRMED' if confirmed else 'rejected'}"
        )
        rows.append(
            {
                "arm": arm_name,
                "phase": phase,
                "plan_hash": plan_hash,
                "status": status,
                "mean_delta_pct": mean,
                "lcb_pct": lcb,
                "ucb_pct": ucb,
                "df": df,
                "reps": len(tuned_tps),
                "stopped_early": _stopped_of(result),
                "confirmed": confirmed,
                "improvement_confident": bool(lcb > min_improvement_pct),
                "paired": paired,
                "reasons": _reasons_of(result),
                "result": result,
                "plan": plan,
            }
        )
    return rows


def format_protocol_feedback(protocol_summary: str, rows: list[dict[str, Any]]) -> str:
    """Build retry-feedback text for the next recommender attempt."""
    lines = [f"Experiment feedback ({protocol_summary}):"]
    for row in rows:
        arm = row.get("arm", "")
        phase = row.get("phase", "")
        mean = _num(row, "mean_delta_pct")
        status = row.get("status", "")
        verdict = "CONFIRMED" if row.get("confirmed") else "rejected"
        lines.append(
            f"- arm {arm} [{phase}]: mean_delta={mean:+.2f}% "
            f"status={status} verdict={verdict}"
        )
    confirmed = sum(1 for row in rows if row.get("confirmed"))
    lines.append(f"Confirmed arms: {confirmed}/{len(rows)}.")
    lines.append(
        "Guidance: vary the failed/rejected arms with fresh knob values; "
        "do not repeat rejected knob values from the arms above."
    )
    lines.append(summarize_arm_results(rows))
    return "\n".join(lines)
