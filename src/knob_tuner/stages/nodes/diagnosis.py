"""Diagnosis enforcement, prompt constraints, and memory helpers.

Coercion of diagnosis payloads, full-history enforcement lookup (Phase 5.4),
prompt-constraint syncing, evidence/belief refresh, and per-knob attribution.
Depends only on :mod:`_common` (leaf helpers live there; none needed here).
"""

from __future__ import annotations

from typing import Any

from src.knob_tuner.stages.evidence import build_evidence_bundle
from src.knob_tuner.stages.models import (
    CorrectionType,
    DiagnosisOutput,
    KnobBelief,
    render_belief_table,
)


# ---------------------------------------------------------------------------
# Wave 2: diagnosis / enforcement / memory helpers
# ---------------------------------------------------------------------------


def _coerce_diagnosis(value: Any) -> DiagnosisOutput | None:
    """Return a :class:`DiagnosisOutput` for diagnosis-shaped inputs, else None."""
    try:
        if isinstance(value, DiagnosisOutput):
            return value
        if isinstance(value, dict) and "correction" in value:
            return DiagnosisOutput.model_validate(value)
        correction = getattr(value, "correction", None)
        if correction is not None and hasattr(value, "model_dump"):
            try:
                return DiagnosisOutput.model_validate(value.model_dump())
            except Exception:
                return None
    except Exception:
        return None
    return None


def _latest_diagnosis(state: Any) -> DiagnosisOutput | None:
    """Return the latest diagnosis from state, or None when inactive.

    ``diagnosis_history`` is authoritative: when the key exists as a list,
    an empty history means inactive (no fallback to ``diagnosis_output``).
    The ``diagnosis_output`` fallback exists only for legacy states without
    a history key.
    """
    try:
        if hasattr(state, "get"):
            hist = state.get("diagnosis_history")
            if isinstance(hist, list):
                if not hist:
                    return None
                return _coerce_diagnosis(hist[-1])
            cur = state.get("diagnosis_output")
            if cur is not None:
                return _coerce_diagnosis(cur)
    except Exception:
        return None
    return None


def _all_diagnoses(state: Any) -> list[DiagnosisOutput]:
    """Return every diagnosis in history order (Phase 5.4 full enforcement).

    ``diagnosis_history`` is authoritative; the ``diagnosis_output`` fallback
    covers legacy states without a history key. Uncoercible entries are
    skipped so one malformed record cannot disable enforcement.
    """
    try:
        if hasattr(state, "get"):
            hist = state.get("diagnosis_history")
            if isinstance(hist, list):
                out: list[DiagnosisOutput] = []
                for entry in hist:
                    diag = _coerce_diagnosis(entry)
                    if diag is not None:
                        out.append(diag)
                return out
            cur = state.get("diagnosis_output")
            diag = _coerce_diagnosis(cur) if cur is not None else None
            return [diag] if diag is not None else []
    except Exception:
        return []
    return []


def _correction_str(diag: DiagnosisOutput) -> str:
    try:
        corr = diag.correction
        return corr.value if isinstance(corr, CorrectionType) else str(corr)
    except Exception:
        return str(getattr(diag, "correction", "") or "")


def _sync_prompt_constraints(state: Any) -> None:
    """Set excluded_knobs/required_phase/max_knobs from the latest diagnosis."""
    try:
        diag = _latest_diagnosis(state)
        if diag is None:
            state["excluded_knobs"] = []
            state["required_phase"] = ""
            state["max_knobs"] = ""
            return
        correction = _correction_str(diag)
        targets = [str(t) for t in (diag.targets or [])]
        if correction == "drop_knob":
            state["excluded_knobs"] = list(targets)
        else:
            state["excluded_knobs"] = []
        if correction == "change_phase" and targets:
            state["required_phase"] = str(targets[0]).strip().lower()
        else:
            state["required_phase"] = ""
        if correction == "shrink_set":
            last_n = None
            try:
                hist = state.get("experiment_history") or []
                if isinstance(hist, list) and hist and isinstance(hist[-1], dict):
                    last_n = int(hist[-1].get("n_knobs") or 0) or None
            except (TypeError, ValueError):
                last_n = None
            if last_n and last_n > 1:
                state["max_knobs"] = last_n - 1
            else:
                state["max_knobs"] = ""
        else:
            state["max_knobs"] = ""
    except Exception:
        pass


def _refresh_memory(state: Any) -> None:
    """Refresh evidence_bundle + belief_table so prompts stay current.

    ``build_evidence_bundle`` only accepts ``Mapping`` states, but the real
    ADK ``State`` is not a ``Mapping`` — pass a plain-dict snapshot of the
    keys the bundle reads so history survives on the live runner.
    """
    try:
        get = getattr(state, "get", None)
        if callable(get):
            snapshot = {
                key: get(key)
                for key in (
                    "experiment_history",
                    "last_screen_row",
                    "rejected_history",
                    "resource_budget",
                    "memory_gb",
                    # R3: canonical-only (legacy "attempt" mirror deleted).
                    "validation_attempt_count",
                    "max_attempts",
                    "plan_hash",
                )
            }
            state["evidence_bundle"] = build_evidence_bundle(snapshot)
        else:
            state["evidence_bundle"] = build_evidence_bundle(state)
    except Exception:
        pass
    try:
        state["belief_table"] = render_belief_table(state.get("knob_beliefs") or {})
    except Exception:
        state["belief_table"] = "No knob beliefs yet."


def _credit_beliefs(
    state: Any, knob_names: list[str], mean_delta: float, phase: str
) -> None:
    """Per-knob attribution: credit verdict mean to each knob, keep best.

    Phase 4.5: read-copy-reassign — never mutate the live state mapping
    in place; the copy is written back so ADK State persistence sees it.
    """
    try:
        current = state.get("knob_beliefs")
        beliefs = dict(current) if isinstance(current, dict) else {}
        for raw_name in knob_names or []:
            name = str(raw_name)
            if not name:
                continue
            cur = beliefs.get(name)
            if isinstance(cur, dict):
                try:
                    best = float(cur.get("best_delta_pct", 0.0) or 0.0)
                except (TypeError, ValueError):
                    best = 0.0
                try:
                    seen = int(cur.get("n_seen", 0) or 0)
                except (TypeError, ValueError):
                    seen = 0
                last_phase = str(cur.get("last_phase", "") or "")
            elif isinstance(cur, KnobBelief):
                best, seen, last_phase = (
                    cur.best_delta_pct,
                    cur.n_seen,
                    cur.last_phase,
                )
            else:
                best, seen, last_phase = 0.0, 0, ""
            try:
                mean_f = float(mean_delta)
            except (TypeError, ValueError):
                mean_f = 0.0
            beliefs[name] = {
                "best_delta_pct": max(best, mean_f),
                "n_seen": seen + 1,
                "last_phase": str(phase or last_phase or ""),
            }
        state["knob_beliefs"] = beliefs
    except Exception:
        pass


def _arm_knobs_for_beliefs(state: Any) -> list[str]:
    """Resolve the arm's knob names from the last row/candidate (never throws)."""
    try:
        for source in (
            (state.get("last_screen_row") or {}).get("plan")
            if isinstance(state.get("last_screen_row"), dict)
            else None,
            (state.get("candidates") or [-1])[-1].get("plan")
            if isinstance(state.get("candidates"), list)
            and state.get("candidates")
            and isinstance((state.get("candidates") or [{}])[-1], dict)
            else None,
            (state.get("all_rows") or [{}])[-1].get("plan")
            if isinstance(state.get("all_rows"), list)
            and state.get("all_rows")
            and isinstance((state.get("all_rows") or [{}])[-1], dict)
            else None,
        ):
            if isinstance(source, dict) and isinstance(source.get("knobs"), list):
                names = [
                    str(spec.get("name"))
                    for spec in source["knobs"]
                    if isinstance(spec, dict) and spec.get("name")
                ]
                if names:
                    return names
    except Exception:
        pass
    return []
