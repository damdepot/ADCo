"""Outcome accounting: audit-trail appends and the attempt counter.

``structure_staging_issues`` is also imported by ``workflow.py`` (decision
and preflight nodes), so it stays re-exported from the package root.
Depends on :mod:`_common` and :mod:`diagnosis`.
"""

from __future__ import annotations

from typing import Any

from src.knob_tuner.contracts import get_min_improvement_pct
from src.knob_tuner.stages.nodes._common import (
    _ensure_list,
    _jsonable,
    _resolve_attempt_cap,
)
from src.knob_tuner.stages.nodes.diagnosis import (
    _arm_knobs_for_beliefs,
    _credit_beliefs,
    _refresh_memory,
    _sync_prompt_constraints,
)


def structure_staging_issues(
    reasons: Any,
    *,
    attempt: int = 0,
    knob_names: list[str] | None = None,
) -> list[str]:
    """Format flat reason strings as structured, deduped audit entries.

    Each entry carries the attempt index and the attributed knob names so a
    manifest reader can answer "what changed, and why" without guessing
    which loop iteration or knob a flat string belonged to. Entries that are
    already structured (start with ``[``) are kept as-is. Duplicates are
    dropped, order-preserving. Never throws.
    """
    try:
        attempt_n = int(attempt or 0)
    except (TypeError, ValueError):
        attempt_n = 0
    knobs: list[str] = []
    try:
        for name in knob_names or []:
            text = str(name or "").strip()
            if text and text not in knobs:
                knobs.append(text)
    except Exception:
        knobs = []
    prefix = ""
    if attempt_n > 0:
        prefix += f"[attempt {attempt_n}]"
    if knobs:
        prefix += f"[knobs {','.join(knobs[:8])}]"
    structured: list[str] = []
    try:
        items = list(reasons or [])
    except TypeError:
        items = [reasons]
    for raw in items:
        text = str(raw or "").strip()
        if not text:
            continue
        if not text.startswith("[") and prefix:
            text = f"{prefix} {text}"
        if text not in structured:
            structured.append(text)
    return structured


def _commit_screen_accounting(
    state: Any,
    *,
    name: str,
    phase: str,
    n_knobs: int,
    status: str,
    mean: float | None,
    lcb: float | None,
    confirmed: bool,
    reasons: list,
    plan_hash: str = "",
    p_win: float | None = None,
    timings: dict[str, Any] | None = None,
) -> int:
    """Append one screen row + bump the attempt counter (called by screen)."""
    experiment_history = _ensure_list(state, "experiment_history")
    rejected_history = _ensure_list(state, "rejected_history")
    row: dict[str, Any] = {
        "name": name,
        "phase": phase or "screen",
        "n_knobs": n_knobs,
        "mean_delta_pct": mean,
        "lcb_pct": lcb,
        "status": status,
        "confirmed": confirmed,
    }
    if plan_hash:
        row["plan_hash"] = plan_hash
    if p_win is not None:
        try:
            row["p_win"] = min(1.0, max(0.0, float(p_win)))
        except (TypeError, ValueError):
            pass
    if timings:
        row["timings"] = _jsonable(timings)
    experiment_history.append(row)
    for reason in reasons or []:
        if reason and reason not in rejected_history:
            rejected_history.append(reason)
    attempt, _ = _resolve_attempt_cap(state)
    new_attempt = attempt + 1
    # No suppression: the attempt counter is the loop's termination
    # condition — if these writes fail, the loop would spin forever, so the
    # failure must propagate to the caller instead of being swallowed.
    # R3: canonical-only (legacy state["attempt"] mirror deleted).
    state["validation_attempt_count"] = new_attempt
    try:
        state["experiments_run"] = int(state.get("experiments_run") or 0) + 1
    except (TypeError, ValueError):
        state["experiments_run"] = len(experiment_history)
    state["experiment_history"] = experiment_history
    state["rejected_history"] = rejected_history
    state["last_failure"] = list(rejected_history)
    cleared = False
    try:
        if lcb is not None:
            cleared = float(lcb) > get_min_improvement_pct(state)
    except (TypeError, ValueError):
        cleared = False
    _credit_beliefs(
        state,
        _arm_knobs_for_beliefs(state),
        mean,
        phase or "screen",
        cleared=cleared,
    )
    _sync_prompt_constraints(state)
    _refresh_memory(state)
    return new_attempt


def _commit_rejection_accounting(
    state: Any, *, reason: str, errors: list | None = None
) -> int:
    """Append one compile rejection + bump the attempt counter."""
    rejected_history = _ensure_list(state, "rejected_history")
    for note in [reason, *(errors or [])]:
        if note and str(note) not in rejected_history:
            rejected_history.append(str(note))
    attempt, _ = _resolve_attempt_cap(state)
    new_attempt = attempt + 1
    # No suppression (see _commit_screen_accounting): the attempt counter is
    # the loop's termination condition, so write failures must propagate.
    # R3: canonical-only (legacy state["attempt"] mirror deleted).
    state["validation_attempt_count"] = new_attempt
    try:
        state["experiments_run"] = int(state.get("experiments_run") or 0) + 1
    except (TypeError, ValueError):
        pass
    state["rejected_history"] = rejected_history
    state["last_failure"] = list(rejected_history)
    _sync_prompt_constraints(state)
    _refresh_memory(state)
    return new_attempt
