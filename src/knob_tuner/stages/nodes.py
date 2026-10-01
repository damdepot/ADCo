"""Wave 2b stage function nodes for the knob-tuner pipeline.

Import-safe standalone functions taking ``(ctx, node_input, ...state params)``
and returning Pydantic outputs (or plain dicts where specified). ``ctx`` is
duck-typed (only ``ctx.state`` — a mutable mapping — and optionally
``ctx.route`` are touched), so nodes run with fake contexts in unit tests.
The staged graph in ``workflow.py`` is authoritative; the
validation/compile/screen/decision logic here backs its nodes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from typing import Any

from src.knob_tuner.contracts import KnobPlan, ResourceBudget
from src.knob_tuner.stages.evidence import build_evidence_bundle
from src.knob_tuner.stages.memory_guard import clamp_memory_knobs
from src.knob_tuner.stages.models import (
    CandidateProposal,
    CompiledPlan,
    CompileRejection,
    CorrectionType,
    DiagnosisOutput,
    KnobBelief,
    PreflightVerdict,
    ScreenVerdict,
    TerminalDecision,
    render_belief_table,
)
from src.knob_tuner.sub_agents.db_inspector.models import DbInspectorOutput
from src.knob_tuner.tools.db_tools import is_noop_value
from src.knob_tuner.tools.experiments import pick_winner, run_experiment_arms
from src.knob_tuner.tools.file_tools import write_json_file
from src.knob_tuner.tools.knobs import build_plan, coerce_profile
from src.knob_tuner.tools.stats import p_win as _welch_p_win

__all__ = [
    "compile_candidate",
    "confirmation_controller",
    "decision",
    "materialize_inventory",
    "prepare_run",
    "production_preflight",
    "screen_candidate",
]

_VALID_EXPERIMENT_PHASES = ("screen", "interaction", "refinement")

# Durability-policy allowlist for the trust boundary.
_DURABILITY_STRICT_VALUES: dict[str, set[str]] = {
    "synchronous_commit": {"on"},
    "full_page_writes": {"on", "true", "1", "yes"},
    "fsync": {"on"},
}


# ---------------------------------------------------------------------------
# Small state helpers (no module globals; pure functions of ctx + inputs)
# ---------------------------------------------------------------------------


def _state(ctx: Any) -> Any:
    state = getattr(ctx, "state", None)
    if isinstance(state, dict):
        return state
    # Real ADK google.adk.sessions.state.State is NOT a dict (no keys() /
    # __iter__), so dict(state) raises and must never be attempted as a copy
    # path — return the live mapping so reads see committed values and writes
    # (state["attempt"] = ..., state["knobs_info"] = ...) persist.
    if hasattr(state, "get") and hasattr(state, "__setitem__"):
        return state
    try:
        return dict(state or {})
    except Exception:
        return {}


def _memory_gb_from_state(state: dict[str, Any]) -> float:
    raw_budget = state.get("resource_budget")
    if isinstance(raw_budget, dict):
        raw_mem = raw_budget.get("memory_gb", state.get("memory_gb", 1.0))
    else:
        raw_mem = state.get("memory_gb", 1.0)
        if raw_budget is not None and hasattr(raw_budget, "memory_gb"):
            try:
                raw_mem = float(raw_budget.memory_gb)
            except (TypeError, ValueError):
                pass
    try:
        return float(raw_mem)
    except (TypeError, ValueError):
        return 1.0


def _inventory_by_name(knobs_info: Any) -> dict[str, dict[str, Any]]:
    """Index a knob inventory list by lowercase name (copy of workflow helper)."""
    inventory: dict[str, dict[str, Any]] = {}
    if isinstance(knobs_info, list):
        for entry in knobs_info:
            if isinstance(entry, dict) and entry.get("name"):
                inventory[str(entry["name"]).lower()] = entry
    return inventory


def _validate_recommendations(
    raw_knobs: list[dict[str, Any]],
    inventory: dict[str, dict[str, Any]],
    durability_profile: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Trust-boundary filter for candidate validation."""
    valid: list[dict[str, Any]] = []
    rejected: list[str] = []
    relaxed = durability_profile == "relaxed"
    for raw in raw_knobs:
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        entry = inventory.get(name.lower())
        if entry is None:
            rejected.append(f"{name}: not in available knob inventory")
            continue
        if str(entry.get("context", "")).strip().lower() == "internal":
            rejected.append(f"{name}: internal/unsettable")
            continue
        value = raw.get("value")
        enumvals = [str(e).strip().lower() for e in (entry.get("enumvals") or [])]
        if enumvals and str(value).strip().lower() not in enumvals:
            rejected.append(f"{name}: {value!r} not in {enumvals}")
            continue
        if not relaxed:
            allowed = _DURABILITY_STRICT_VALUES.get(name.lower())
            if allowed is not None and str(value).strip().lower() not in allowed:
                rejected.append(f"{name}: durability policy 'strict' forbids {value!r}")
                continue
        if is_noop_value(entry, value):
            rejected.append(
                f"{name}: {value!r} equals the current value (no-op, nothing to change)"
            )
            continue
        valid.append(raw)
    return valid, rejected


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


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


def _correction_str(diag: DiagnosisOutput) -> str:
    try:
        corr = diag.correction
        return corr.value if isinstance(corr, CorrectionType) else str(corr)
    except Exception:
        return str(getattr(diag, "correction", "") or "")


def _previous_plan_knobs(state: Any) -> dict[str, Any]:
    """Return ``{lower_name: value}`` for the most recent pre-proposal plan.

    Looks up the last screen row's ``plan_hash`` in ``candidates``/``all_rows``;
    falls back to the last available plan dump when no hash matches.
    """
    try:
        last_row = state.get("last_screen_row") if hasattr(state, "get") else None
        want_hash = last_row.get("plan_hash") if isinstance(last_row, dict) else None
        for key in ("candidates", "all_rows"):
            items = state.get(key) if hasattr(state, "get") else None
            if not isinstance(items, list):
                continue
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                if want_hash and entry.get("plan_hash") != want_hash:
                    continue
                plan = entry.get("plan")
                if isinstance(plan, dict) and isinstance(plan.get("knobs"), list):
                    out: dict[str, Any] = {}
                    for spec in plan["knobs"]:
                        if isinstance(spec, dict) and spec.get("name") is not None:
                            out[str(spec["name"]).strip().lower()] = spec.get("value")
                    if out and (not want_hash or entry.get("plan_hash") == want_hash):
                        if want_hash:
                            return out
            # Fallthrough when no hash match: use last plan dump below.
        for key in ("candidates", "all_rows"):
            items = state.get(key) if hasattr(state, "get") else None
            if isinstance(items, list) and items:
                for entry in reversed(items):
                    if isinstance(entry, dict):
                        plan = entry.get("plan")
                        if isinstance(plan, dict) and isinstance(
                            plan.get("knobs"), list
                        ):
                            return {
                                str(spec["name"]).strip().lower(): spec.get("value")
                                for spec in plan["knobs"]
                                if isinstance(spec, dict)
                                and spec.get("name") is not None
                            }
        if isinstance(last_row, dict):
            plan = last_row.get("plan")
            if isinstance(plan, dict) and isinstance(plan.get("knobs"), list):
                return {
                    str(spec["name"]).strip().lower(): spec.get("value")
                    for spec in plan["knobs"]
                    if isinstance(spec, dict) and spec.get("name") is not None
                }
    except Exception:
        return {}
    return {}


def _known_plan_hashes(state: Any) -> set[str]:
    """Collect plan hashes from experiment_history (repeat guard)."""
    known: set[str] = set()
    try:
        hist = state.get("experiment_history") if hasattr(state, "get") else None
        if isinstance(hist, list):
            for entry in hist:
                if isinstance(entry, dict) and entry.get("plan_hash"):
                    known.add(str(entry["plan_hash"]))
    except Exception:
        pass
    return known


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
                    "attempt",
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
    """Per-knob attribution: credit verdict mean to each knob, keep best."""
    try:
        beliefs = state.get("knob_beliefs")
        if not isinstance(beliefs, dict):
            beliefs = state["knob_beliefs"] = {}
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


# ---------------------------------------------------------------------------
# Loop accounting (attempt counting lives in the outcome producers)
# ---------------------------------------------------------------------------


def _resolve_attempt_cap(state: Any) -> tuple[int, int]:
    """Read ``(attempt, max_attempts)`` WITHOUT incrementing (pure read)."""
    try:
        attempt = int(
            state.get("attempt", state.get("validation_attempt_count", 0)) or 0
        )
    except (TypeError, ValueError):
        attempt = 0
    try:
        raw_max = state.get("max_attempts", 10)
        max_attempts = max(1, int(raw_max or 10))
    except (TypeError, ValueError):
        max_attempts = 10
    return attempt, max(1, max_attempts)


def _min_improvement_pct(state: Any) -> float:
    try:
        return float(state.get("min_improvement_pct", 5.0) or 5.0)
    except (TypeError, ValueError):
        return 5.0


def _as_float_list(value: Any) -> list[float]:
    """Coerce a sample list to floats, dropping non-numeric entries."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[float] = []
    for item in value:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


def _paired_side_samples(paired: Any, key: str) -> list[float]:
    """Extract per-run samples for one paired side (never throws)."""
    try:
        if paired is None:
            return []
        if isinstance(paired, dict):
            side = paired.get(key)
        else:
            side = getattr(paired, key, None)
        if side is None:
            return []
        if isinstance(side, dict):
            raw = side.get("per_run_tps")
        else:
            raw = getattr(side, "per_run_tps", None)
        return _as_float_list(raw or [])
    except Exception:  # noqa: BLE001 - sample extraction never throws
        return []


def _p_win_of_paired(paired: Any) -> float | None:
    """One-sided Welch P(win) from paired samples, or None when unusable."""
    try:
        baseline = _paired_side_samples(paired, "baseline")
        tuned = _paired_side_samples(paired, "tuned")
        if len(baseline) < 2 or len(tuned) < 2:
            return None
        return _welch_p_win(baseline, tuned)
    except Exception:  # noqa: BLE001 - P(win) lookup never throws
        return None


def _iter_verdict_rows(state: Any, node_input: Any = None) -> list[dict[str, Any]]:
    """Collect screen-verdict-shaped rows that may carry paired samples."""
    rows: list[dict[str, Any]] = []

    def _consider(value: Any) -> None:
        try:
            if isinstance(value, ScreenVerdict):
                rows.append(value.model_dump())
            elif isinstance(value, dict) and ("status" in value or "paired" in value):
                rows.append(value)
        except Exception:  # noqa: BLE001, S110 - row collection never throws
            pass

    _consider(node_input)
    try:
        get = getattr(state, "get", None)
        if callable(get):
            _consider(get("last_screen_row"))
            all_rows = get("all_rows")
            if isinstance(all_rows, list):
                for entry in all_rows:
                    _consider(entry)
            candidates = get("candidates")
            if isinstance(candidates, list):
                for cand in candidates:
                    if isinstance(cand, dict) and cand.get("paired") is not None:
                        _consider(cand)
    except Exception:  # noqa: BLE001, S110 - row collection never throws
        pass
    return rows


def _best_confirmed_p_win(
    state: Any, node_input: Any = None
) -> tuple[float | None, bool]:
    """Return ``(p_win, found)`` for the best confirmed row (never throws).

    The best row is the confirmed row with the highest ``mean_delta_pct``
    that carries usable paired samples. When no confirmed row has samples,
    falls back to the latest verdict-shaped row with usable samples (an
    unconfirmed failure still bounds the win probability for futility
    checks). ``(None, False)`` when nothing usable exists.
    """
    try:
        rows = _iter_verdict_rows(state, node_input)
        best: tuple[float, float] | None = None
        for row in rows:
            if not bool(row.get("confirmed", False)):
                continue
            prob = _p_win_of_paired(row.get("paired"))
            if prob is None:
                continue
            try:
                mean = float(row.get("mean_delta_pct", 0.0) or 0.0)
            except (TypeError, ValueError):
                mean = 0.0
            if best is None or mean > best[0]:
                best = (mean, prob)
        if best is not None:
            return best[1], True
        for row in reversed(rows):
            prob = _p_win_of_paired(row.get("paired"))
            if prob is not None:
                return prob, True
    except Exception:  # noqa: BLE001, S110 - gate stat lookup never throws
        pass
    return None, False


def _diag_confidence(diag: DiagnosisOutput) -> float:
    """Return the diagnosis confidence in [0, 1] (never throws)."""
    try:
        return min(1.0, max(0.0, float(getattr(diag, "confidence", 0.0) or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _stop_reason_str(diag: DiagnosisOutput) -> str:
    """Return 'winner' or 'futility' for a stop diagnosis (never throws)."""
    try:
        reason = str(getattr(diag, "stop_reason", "futility") or "futility")
        reason = reason.strip().lower()
        if reason in ("winner", "futility"):
            return reason
    except Exception:  # noqa: BLE001, S110 - reason lookup never throws
        pass
    return "futility"


def _gate_thresholds(state: Any) -> tuple[float, float, float]:
    """Return ``(stop_diag_min, stop_stat_win_min, futility_stat_max)``.

    Read from state keys of the same name with defaults 0.6 / 0.7 / 0.4;
    unparseable values fall back to the defaults and all values clamp to
    [0, 1]. Never throws.
    """
    defaults = (0.6, 0.7, 0.4)
    keys = ("stop_diag_min", "stop_stat_win_min", "futility_stat_max")
    out: list[float] = []
    for key, default in zip(keys, defaults):
        try:
            get = getattr(state, "get", None)
            raw = get(key) if callable(get) else None
            value = float(raw) if raw is not None else default
        except (TypeError, ValueError):
            value = default
        out.append(min(1.0, max(0.0, value)))
    return out[0], out[1], out[2]


def _ensure_list(state: Any, key: str) -> list:
    items = state.get(key) if hasattr(state, "get") else None
    if not isinstance(items, list):
        items = []
        with contextlib.suppress(Exception):
            state[key] = items
    return items


def _commit_screen_accounting(
    state: Any,
    *,
    name: str,
    phase: str,
    n_knobs: int,
    status: str,
    mean: float,
    lcb: float,
    confirmed: bool,
    reasons: list,
    plan_hash: str = "",
    p_win: float | None = None,
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
    experiment_history.append(row)
    for reason in reasons or []:
        if reason and reason not in rejected_history:
            rejected_history.append(reason)
    attempt, _ = _resolve_attempt_cap(state)
    new_attempt = attempt + 1
    with contextlib.suppress(Exception):
        state["attempt"] = new_attempt
        state["validation_attempt_count"] = new_attempt
        state["experiment_history"] = experiment_history
        state["rejected_history"] = rejected_history
        state["last_failure"] = list(rejected_history)
    _credit_beliefs(state, _arm_knobs_for_beliefs(state), mean, phase or "screen")
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
    with contextlib.suppress(Exception):
        state["attempt"] = new_attempt
        state["validation_attempt_count"] = new_attempt
        state["rejected_history"] = rejected_history
        state["last_failure"] = list(rejected_history)
    _sync_prompt_constraints(state)
    _refresh_memory(state)
    return new_attempt


def _describe_arm(node_input: Any, state: Any) -> tuple[str, str, int]:
    """Return ``(name, phase, n_knobs)`` for a screen arm (never throws)."""
    name, phase, n_knobs = "experiment", "screen", 0
    with contextlib.suppress(Exception):
        if isinstance(node_input, CompiledPlan):
            name = node_input.exp_name or name
            phase = node_input.phase or phase
            plan = node_input.plan
            if isinstance(plan, dict) and isinstance(plan.get("knobs"), list):
                n_knobs = len(plan["knobs"])
        elif isinstance(node_input, dict):
            name = (
                str(
                    node_input.get("exp_name", node_input.get("arm", node_input.get("name", name)))
                    or name
                )
            )
            phase = str(node_input.get("phase", phase) or phase)
            plan = node_input.get("plan")
            if isinstance(plan, dict) and isinstance(plan.get("knobs"), list):
                n_knobs = len(plan["knobs"])
            if not n_knobs:
                with contextlib.suppress(TypeError, ValueError):
                    n_knobs = int(node_input.get("n_knobs") or 0)
        else:
            name = str(getattr(node_input, "exp_name", name) or name)
            phase = str(getattr(node_input, "phase", phase) or phase)
        if not n_knobs:
            with contextlib.suppress(TypeError, ValueError):
                n_knobs = int(state.get("n_knobs") or 0)
    return name or "experiment", phase or "screen", n_knobs


def _verdict_fields(verdict: Any) -> tuple[str, float, float, bool, list]:
    """Return ``(status, mean, lcb, confirmed, reasons)`` (never throws)."""
    status, mean, lcb, confirmed, reasons = "FAIL", 0.0, 0.0, False, []
    with contextlib.suppress(Exception):
        if isinstance(verdict, ScreenVerdict):
            status = verdict.status
            mean = float(verdict.mean_delta_pct or 0.0)
            lcb = float(verdict.lcb_pct or 0.0)
            confirmed = bool(verdict.confirmed)
            reasons = list(verdict.reasons or [])
        elif isinstance(verdict, dict):
            status = str(verdict.get("status", "FAIL"))
            with contextlib.suppress(TypeError, ValueError):
                mean = float(verdict.get("mean_delta_pct", 0.0) or 0.0)
            with contextlib.suppress(TypeError, ValueError):
                lcb = float(verdict.get("lcb_pct", 0.0) or 0.0)
            confirmed = bool(verdict.get("confirmed", False))
            reasons = [str(r) for r in (verdict.get("reasons") or [])]
    return status, mean, lcb, confirmed, reasons


def _latest_verdict_stats(state: Any, node_input: Any = None) -> tuple[float, float, bool]:
    """Return ``(mean, lcb, found)`` for the latest screen verdict (never throws)."""
    with contextlib.suppress(Exception):
        value = node_input
        if isinstance(value, ScreenVerdict):
            return float(value.mean_delta_pct or 0.0), float(value.lcb_pct or 0.0), True
        if isinstance(value, dict) and "status" in value and (
            "mean_delta_pct" in value or "lcb_pct" in value
        ):
            return (
                float(value.get("mean_delta_pct") or 0.0),
                float(value.get("lcb_pct") or 0.0),
                True,
            )
        row = state.get("last_screen_row")
        if isinstance(row, dict) and row.get("status"):
            return (
                float(row.get("mean_delta_pct") or 0.0),
                float(row.get("lcb_pct") or 0.0),
                True,
            )
        hist = state.get("experiment_history")
        if isinstance(hist, list) and hist and isinstance(hist[-1], dict):
            last = hist[-1]
            if "mean_delta_pct" in last or "lcb_pct" in last:
                return (
                    float(last.get("mean_delta_pct") or 0.0),
                    float(last.get("lcb_pct") or 0.0),
                    True,
                )
    return 0.0, 0.0, False


# ---------------------------------------------------------------------------
# 1. materialize_inventory
# ---------------------------------------------------------------------------


def materialize_inventory(ctx: Any, node_input: Any) -> dict[str, Any]:
    """Persist a :class:`DbInspectorOutput` to state + ``{knob_path}/knobs.json``.

    Replicates the state effects of ``extract_knobs`` + ``write_knobs_file``
    without agent tools. Never returns ``None``.
    """
    state = _state(ctx)
    try:
        if isinstance(node_input, DbInspectorOutput):
            output = node_input
        elif isinstance(node_input, dict):
            output = DbInspectorOutput.model_validate(node_input)
        elif hasattr(node_input, "model_dump"):
            output = DbInspectorOutput.model_validate(node_input.model_dump())
        else:
            return {"error": f"unsupported DbInspectorOutput shape: {type(node_input)}"}

        knobs_info = [k.model_dump() for k in output.available_knobs]
        available = sorted(
            k.name for k in output.available_knobs if k.context.strip().lower() != "internal"
        )
        schema_info = [t.model_dump() for t in output.tables]

        knob_path = str(state.get("knob_path") or state.get("target") or ".")
        out_file = os.path.join(knob_path, "knobs.json")
        try:
            write_json_file(out_file, knobs_info)
            write_status = f"OK: wrote {len(knobs_info)} knobs to {out_file}"
        except Exception as exc:
            return {"error": f"failed to write knobs file: {exc}"}

        state["knobs_info"] = knobs_info
        state["available_knob_names"] = available
        state["schema_info"] = schema_info
        state["db_version"] = output.db_version
        state["db_type"] = output.db_type
        return {
            "status": write_status,
            "knob_file": out_file,
            "n_knobs": len(knobs_info),
            "n_available": len(available),
            "n_tables": len(schema_info),
            "db_version": output.db_version,
        }
    except Exception as exc:
        return {"error": f"materialize_inventory failed: {exc}"}


# ---------------------------------------------------------------------------
# 3. compile_candidate
# ---------------------------------------------------------------------------


def _split_design(node_input: Any) -> tuple[str, Any, Any, str, str]:
    """Return ``(exp_name, phase_raw, raw_levels, rationale, objective)``."""
    if isinstance(node_input, dict):
        return (
            str(node_input.get("name") or "experiment"),
            node_input.get("phase") or "",
            node_input.get("levels") or [],
            str(node_input.get("rationale") or ""),
            str(node_input.get("objective") or node_input.get("summary") or ""),
        )
    if isinstance(node_input, CandidateProposal):
        return (
            node_input.name,
            node_input.phase,
            [lvl.model_dump() for lvl in node_input.levels],
            node_input.rationale,
            node_input.objective,
        )
    return (
        str(getattr(node_input, "name", "") or "experiment"),
        getattr(node_input, "phase", "") or "",
        getattr(node_input, "levels", None) or [],
        str(getattr(node_input, "rationale", "") or ""),
        str(
            getattr(node_input, "objective", None)
            or getattr(node_input, "summary", None)
            or ""
        ),
    )


def _normalize_levels(raw_levels: Any) -> list[dict[str, Any]]:
    norm: list[dict[str, Any]] = []
    for lvl in raw_levels or []:
        if isinstance(lvl, dict):
            value = lvl.get("value", lvl.get("recommended_value"))
            if "value" not in lvl and "recommended_value" not in lvl:
                value = None
            knob = lvl.get("knob", lvl.get("name", lvl.get("knob_name", "")))
            reasoning = str(lvl.get("reasoning", "") or "")
            restart = bool(lvl.get("restart_required", False))
        else:
            knob = getattr(
                lvl, "knob", getattr(lvl, "name", getattr(lvl, "knob_name", ""))
            )
            if hasattr(lvl, "model_dump"):
                dumped = lvl.model_dump()
                value = dumped.get("value", dumped.get("recommended_value"))
                reasoning = str(dumped.get("reasoning", "") or "")
                restart = bool(dumped.get("restart_required", False))
            else:
                value = getattr(
                    lvl, "value", getattr(lvl, "recommended_value", None)
                )
                reasoning = str(getattr(lvl, "reasoning", "") or "")
                restart = bool(getattr(lvl, "restart_required", False))
        if not knob or value is None:
            continue
        norm.append(
            {
                "knob": str(knob),
                "value": value,
                "reasoning": reasoning,
                "restart_required": restart,
            }
        )
    return norm


def _compile_candidate_inner(
    ctx: Any,
    node_input: Any,
    max_set_knobs: int | None = None,
    durability_profile: str | None = None,
    knobs_info: list | None = None,
    context_map: dict | None = None,
) -> CompiledPlan | CompileRejection:
    """Validate one proposed experiment and compile it to a :class:`KnobPlan`.

    Rejections are returned as :class:`CompileRejection` values, never ``None``.
    Memory comes from ``resource_budget.memory_gb`` in state. This inner
    function performs validation only — the public :func:`compile_candidate`
    wrapper owns rejection accounting (``rejected_history`` append + attempt
    bump + memory refresh) so compile errors retry cheaply without an LLM call.
    """
    state = _state(ctx)
    try:
        if max_set_knobs is None:
            try:
                max_set_knobs = max(1, int(state.get("max_set_knobs", 20) or 20))
            except (TypeError, ValueError):
                max_set_knobs = 20
        if durability_profile is None:
            durability_profile = (
                str(state.get("durability_profile", "strict") or "strict").strip().lower()
            )
        if knobs_info is None:
            knobs_info = state.get("knobs_info") or []
        if context_map is None:
            context_map = (
                state.get("context_map") or state.get("pg_context_map") or {}
            )
        memory_gb = _memory_gb_from_state(state)

        exp_name, phase_raw, raw_levels, _rationale, _objective = _split_design(node_input)
        norm_levels = _normalize_levels(raw_levels)
        if not norm_levels:
            return CompileRejection(
                reason="no usable knobs (empty levels)",
                errors=[f"experiment {exp_name!r}: no usable knobs (empty levels)"],
                design_name=exp_name,
            )
        phase = str(phase_raw or "").strip().lower()
        if phase not in _VALID_EXPERIMENT_PHASES:
            return CompileRejection(
                reason=f"unknown phase {phase_raw!r}",
                errors=[f"experiment {exp_name!r}: unknown phase {phase_raw!r}"],
                design_name=exp_name,
            )
        distinct = {str(lvl["knob"]).lower() for lvl in norm_levels}
        if len(distinct) > max_set_knobs:
            note = (
                f"experiment {exp_name!r}: distinct knobs "
                f"{len(distinct)} above cap {max_set_knobs}"
            )
            return CompileRejection(
                reason=note, errors=[note], design_name=exp_name
            )

        raw_for_validation = [
            {
                "name": lvl["knob"],
                "value": lvl["value"],
                "restart_required": lvl.get("restart_required", False),
            }
            for lvl in norm_levels
        ]
        inventory = _inventory_by_name(knobs_info)
        valid_knobs, inv_rejected = _validate_recommendations(
            raw_for_validation, inventory, durability_profile
        )
        if not valid_knobs:
            errors = list(inv_rejected) or [f"experiment {exp_name!r}: no usable knobs"]
            return CompileRejection(
                reason="no usable knobs after inventory validation",
                errors=errors,
                design_name=exp_name,
            )

        clamped = clamp_memory_knobs(
            [dict(v) for v in valid_knobs], memory_gb
        )
        plan = build_plan(clamped, dict(context_map or {}))
        if not plan.knobs:
            return CompileRejection(
                reason="no usable knobs (plan build dropped all knobs)",
                errors=[f"experiment {exp_name!r}: no usable knobs"],
                design_name=exp_name,
            )
        # Wave 2: per-enum hard enforcement against the latest diagnosis.
        diag = _latest_diagnosis(state)
        if diag is not None:
            correction = _correction_str(diag)
            targets = [str(t) for t in (diag.targets or [])]
            targets_lower = {t.strip().lower() for t in targets if t.strip()}
            plan_names = [str(spec.name) for spec in plan.knobs]
            plan_lower = {n.strip().lower() for n in plan_names}
            distinct_n = len({n.strip().lower() for n in plan_names})
            if correction == "stop":
                return CompileRejection(
                    reason=f"stop: halted by diagnosis ({diag.rationale or 'no rationale'})",
                    errors=[f"stop: {diag.rationale or 'halt requested'}"],
                    design_name=exp_name,
                )
            if correction == "drop_knob" and targets_lower:
                hit = sorted(plan_lower & targets_lower)
                if hit:
                    return CompileRejection(
                        reason=f"drop_knob violation: proposal includes excluded knob(s) {hit}",
                        errors=[
                            f"drop_knob: {', '.join(hit)} must be excluded per diagnosis"
                        ],
                        design_name=exp_name,
                    )
            elif correction == "shrink_set":
                last_n: int | None = None
                try:
                    hist = state.get("experiment_history") or []
                    if (
                        isinstance(hist, list)
                        and hist
                        and isinstance(hist[-1], dict)
                    ):
                        last_n = int(hist[-1].get("n_knobs") or 0) or None
                except (TypeError, ValueError):
                    last_n = None
                if last_n is not None and distinct_n >= last_n:
                    return CompileRejection(
                        reason=(
                            "shrink_set violation: proposal has "
                            f"{distinct_n} knobs, must be fewer than last "
                            f"attempt n_knobs={last_n}"
                        ),
                        errors=[
                            f"shrink_set: {distinct_n} >= {last_n}; drop to fewer knobs"
                        ],
                        design_name=exp_name,
                    )
            elif correction == "change_phase":
                if targets:
                    want = str(targets[0]).strip().lower()
                    if phase != want:
                        return CompileRejection(
                            reason=(
                                "change_phase violation: proposal phase "
                                f"{phase!r} != required {want!r}"
                            ),
                            errors=[f"change_phase: use required phase {want!r}"],
                            design_name=exp_name,
                        )
            elif correction == "adjust_value" and targets_lower:
                new_map = {
                    str(spec.name).strip().lower(): spec.value
                    for spec in plan.knobs
                }
                prev_map = _previous_plan_knobs(state)
                problems: list[str] = []
                for target in sorted(targets_lower):
                    if target not in new_map:
                        problems.append(f"{target} missing from proposal")
                    elif target in prev_map and str(
                        new_map[target]
                    ).strip().lower() == str(prev_map[target]).strip().lower():
                        problems.append(
                            f"{target} value unchanged ({new_map[target]!r})"
                        )
                if problems:
                    return CompileRejection(
                        reason=(
                            "adjust_value violation: " + "; ".join(problems)
                        ),
                        errors=[
                            f"adjust_value: {p}" for p in problems
                        ],
                        design_name=exp_name,
                    )
        # Wave 2: repeat guard — reject already-seen plan hashes.
        try:
            proposed_hash = plan.plan_hash()
            retry_active = (
                diag is not None and _correction_str(diag) == "retry_same"
            )
            if not retry_active and proposed_hash in _known_plan_hashes(state):
                return CompileRejection(
                    reason=(
                        f"repeat plan: hash {proposed_hash[:12]} already in "
                        "experiment_history (retry_same is the only exception)"
                    ),
                    errors=[f"repeat plan_hash {proposed_hash}"],
                    design_name=exp_name,
                )
        except Exception:
            pass
        return CompiledPlan(
            plan=plan.model_dump(),
            exp_name=exp_name,
            phase=phase,
            valid_knobs=[spec.name for spec in plan.knobs],
        )
    except Exception as exc:
        name = "experiment"
        try:
            name = str(_split_design(node_input)[0])
        except Exception:
            pass
        return CompileRejection(
            reason=f"compile error: {exc}", errors=[str(exc)], design_name=name
        )


def compile_candidate(
    ctx: Any,
    node_input: Any,
    max_set_knobs: int | None = None,
    durability_profile: str | None = None,
    knobs_info: list | None = None,
    context_map: dict | None = None,
) -> CompiledPlan | CompileRejection:
    """Validate one proposal; rejections append + bump attempt here.

    Compile successes are NOT counted — the downstream
    :func:`screen_candidate` records the attempt when the plan runs. Compile
    rejections route straight to the controller (bypassing diagnosis) and are
    recorded here so the controller stays a pure router.
    """
    state = _state(ctx)
    result = _compile_candidate_inner(
        ctx,
        node_input,
        max_set_knobs=max_set_knobs,
        durability_profile=durability_profile,
        knobs_info=knobs_info,
        context_map=context_map,
    )
    if isinstance(result, CompileRejection):
        with contextlib.suppress(Exception):
            _commit_rejection_accounting(
                state, reason=result.reason, errors=list(result.errors or [])
            )
    return result


# ---------------------------------------------------------------------------
# 4. screen_candidate
# ---------------------------------------------------------------------------


def _baseline_cache_key(run_profile: Any, dataset: Any) -> str:
    try:
        canonical = json.dumps(
            {"profile": _jsonable(run_profile), "dataset": _jsonable(dataset)},
            sort_keys=True,
            default=str,
        )
    except Exception:
        canonical = f"{run_profile!r}|{dataset!r}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _coerce_plan(node_input: Any) -> KnobPlan | None:
    try:
        if isinstance(node_input, KnobPlan):
            return node_input
        if isinstance(node_input, CompiledPlan):
            return KnobPlan.model_validate(node_input.plan)
        if isinstance(node_input, dict) and "plan" in node_input:
            return KnobPlan.model_validate(node_input["plan"])
        if isinstance(node_input, dict) and "knobs" in node_input:
            return KnobPlan.model_validate(node_input)
        if hasattr(node_input, "model_dump"):
            dumped = node_input.model_dump()
            if isinstance(dumped, dict) and "plan" in dumped:
                return KnobPlan.model_validate(dumped["plan"])
    except Exception:
        return None
    return None


def _coerce_paired(paired: Any) -> dict[str, Any]:
    if paired is None:
        return {}
    if isinstance(paired, dict):
        return paired
    if hasattr(paired, "model_dump"):
        try:
            dumped = paired.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    return {"repr": str(paired)}


def _fail_verdict(reason: str) -> ScreenVerdict:
    return ScreenVerdict(
        status="FAIL",
        mean_delta_pct=0.0,
        lcb_pct=0.0,
        ucb_pct=0.0,
        confirmed=False,
        improvement_confident=False,
        stopped_early=False,
        paired={},
        reasons=[reason],
    )


def _screen_candidate_inner(
    ctx: Any,
    node_input: Any,
    validate_fn: Any = None,
    run_profile: Any = None,
    attempt: int | None = None,
    early_stop_min_reps: int | None = None,
    min_improvement_pct: float | None = None,
    shared_baseline: Any = None,
) -> ScreenVerdict:
    """Screen one compiled plan against the shared baseline (no accounting).

    The baseline is measured once and cached in state (keyed by the
    dataset/profile hash). Always returns a :class:`ScreenVerdict`, never
    ``None`` (a fail value on error). The public :func:`screen_candidate`
    wrapper owns outcome accounting (``experiment_history`` row + attempt
    bump + beliefs/evidence refresh) so every screen verdict — pass or fail —
    is recorded exactly once before diagnosis.
    """
    state = _state(ctx)
    try:
        if validate_fn is None:
            validate_fn = state.get("validate_fn")
        if validate_fn is None:
            return _fail_verdict("screen_candidate: no validate_fn available")
        if run_profile is None:
            run_profile = state.get("run_profile") or state.get("candidate_run_profile")
        if attempt is None:
            try:
                attempt = int(state.get("attempt", state.get("validation_attempt_count", 0)) or 0)
            except (TypeError, ValueError):
                attempt = 0
        if early_stop_min_reps is None:
            try:
                early_stop_min_reps = max(
                    2, int(state.get("early_stop_min_reps", 4) or 4)
                )
            except (TypeError, ValueError):
                early_stop_min_reps = 4
        if min_improvement_pct is None:
            try:
                min_improvement_pct = float(state.get("min_improvement_pct", 5.0) or 5.0)
            except (TypeError, ValueError):
                min_improvement_pct = 5.0

        plan = _coerce_plan(node_input)
        if plan is None or not plan.knobs:
            return _fail_verdict("screen_candidate: empty or invalid compiled plan")
        if isinstance(node_input, CompiledPlan):
            phase, exp_name = node_input.phase, node_input.exp_name
        elif isinstance(node_input, dict):
            phase = str(node_input.get("phase", "screen") or "screen")
            exp_name = str(node_input.get("exp_name", node_input.get("arm", "experiment")))
        else:
            phase = str(getattr(node_input, "phase", "screen") or "screen")
            exp_name = str(getattr(node_input, "exp_name", "experiment"))

        dataset = state.get("screen_dataset") or state.get("dataset")
        cache_key = _baseline_cache_key(run_profile, dataset)
        if shared_baseline is None:
            cached = state.get("baseline", state.get("shared_baseline"))
            if cached is not None and state.get("baseline_cache_key", cache_key) != cache_key:
                cached = None
            shared_baseline = cached
        if shared_baseline is None:
            baseline_result = validate_fn(
                plan=KnobPlan(knobs=[]),
                run_profile=run_profile,
                shared_baseline=None,
                attempt=attempt,
                early_stop_min_reps=early_stop_min_reps,
                baseline_only=True,
            )
            if isinstance(baseline_result, dict):
                shared_baseline = baseline_result.get("baseline")
            else:
                shared_baseline = getattr(baseline_result, "baseline", None)
            state["baseline"] = shared_baseline
            state["shared_baseline"] = shared_baseline
            state["baseline_cache_key"] = cache_key
        else:
            state.setdefault("baseline", shared_baseline)
            state.setdefault("shared_baseline", shared_baseline)
            state.setdefault("baseline_cache_key", cache_key)

        rows = run_experiment_arms(
            arms=[(plan, phase, exp_name)],
            shared_baseline=shared_baseline,
            validate_fn=validate_fn,
            run_profile=run_profile,
            attempt_base=int(attempt or 0),
            early_stop_min_reps=early_stop_min_reps,
            progress=None,
            min_improvement_pct=float(min_improvement_pct),
        )
        if not rows:
            return _fail_verdict(f"screen_candidate: no result rows for {exp_name!r}")
        row = rows[0]
        plan_dump = _jsonable(plan)
        rows_lite = dict(row)
        rows_lite["plan"] = plan_dump
        rows_lite.pop("result", None)
        state.setdefault("all_rows", []).append(_jsonable(rows_lite))
        state["last_screen_row"] = _jsonable(rows_lite)
        state.setdefault("candidates", []).append(
            {
                "plan": plan_dump,
                "plan_hash": row.get("plan_hash"),
                "result": {"paired": _jsonable(row.get("paired"))},
                "paired": _jsonable(row.get("paired")),
            }
        )
        return ScreenVerdict(
            status=str(row.get("status", "FAIL") or "FAIL"),
            mean_delta_pct=float(row.get("mean_delta_pct", 0.0) or 0.0),
            lcb_pct=float(row.get("lcb_pct", 0.0) or 0.0),
            ucb_pct=float(row.get("ucb_pct", 0.0) or 0.0),
            confirmed=bool(row.get("confirmed", False)),
            improvement_confident=bool(row.get("improvement_confident", False)),
            stopped_early=bool(row.get("stopped_early", False)),
            paired=_coerce_paired(row.get("paired")),
            reasons=[str(r) for r in (row.get("reasons") or [])],
        )
    except Exception as exc:
        return _fail_verdict(f"screen_candidate error: {exc}")


def screen_candidate(
    ctx: Any,
    node_input: Any,
    validate_fn: Any = None,
    run_profile: Any = None,
    attempt: int | None = None,
    early_stop_min_reps: int | None = None,
    min_improvement_pct: float | None = None,
    shared_baseline: Any = None,
) -> ScreenVerdict:
    """Screen one compiled plan and record the outcome exactly once.

    Appends the verdict row to ``experiment_history`` (plus reasons to
    ``rejected_history``), bumps the attempt counter, credits per-knob
    beliefs, and refreshes the evidence bundle — for BOTH pass and fail
    verdicts. The downstream ``diagnosis_agent`` and
    ``confirmation_controller`` never re-count, so a
    screen → diagnosis → controller sequence increments the attempt exactly
    once. Always returns a :class:`ScreenVerdict`, never ``None``.
    """
    state = _state(ctx)
    verdict = _screen_candidate_inner(
        ctx,
        node_input,
        validate_fn=validate_fn,
        run_profile=run_profile,
        attempt=attempt,
        early_stop_min_reps=early_stop_min_reps,
        min_improvement_pct=min_improvement_pct,
        shared_baseline=shared_baseline,
    )
    with contextlib.suppress(Exception):
        name, phase, n_knobs = _describe_arm(node_input, state)
        status, mean, lcb, confirmed, reasons = _verdict_fields(verdict)
        plan_hash = ""
        last_row = state.get("last_screen_row")
        if isinstance(last_row, dict) and last_row.get("plan_hash"):
            plan_hash = str(last_row.get("plan_hash"))
        paired = verdict.paired if isinstance(verdict, ScreenVerdict) else None
        _commit_screen_accounting(
            state,
            name=name,
            phase=phase,
            n_knobs=n_knobs,
            status=status,
            mean=mean,
            lcb=lcb,
            confirmed=confirmed,
            reasons=reasons,
            plan_hash=plan_hash,
            p_win=_p_win_of_paired(paired),
        )
    return verdict


# ---------------------------------------------------------------------------
# 5. confirmation_controller
# ---------------------------------------------------------------------------


def confirmation_controller(
    ctx: Any,
    node_input: Any,
    attempt: int | None = None,
    max_attempts: int | None = None,
    exp_name: str = "",
    phase: str = "",
    n_knobs: int = 0,
) -> dict[str, Any]:
    """Pure router: route ``"retry"``/``"done"`` without counting attempts.

    Outcome accounting lives in the producers — :func:`screen_candidate`
    appends its verdict row and :func:`compile_candidate` appends its
    rejection (each bumping the attempt counter exactly once). This controller
    never increments the attempt counter and never appends to
    ``experiment_history``/``rejected_history``, so extra controller visits
    cannot double-count. Diagnosis arrivals still append to
    ``diagnosis_history`` (bookkeeping only, no counting) and refresh the
    prompt constraints/memory so the next proposal sees the correction.
    Routes on the latest diagnosis correction plus attempt-vs-cap plus a
    confident-win backstop. A ``stop`` diagnosis halts only through the
    two-score gate (diagnosis confidence + Welch P(win) must back the
    stated stop_reason); disagreement routes retry with
    ``diag_stat_disagree``. The output carries ``diag_confidence``,
    ``stat_p_win``, ``stop_reason``, ``gate``, and the ``thresholds`` used.
    Never returns ``None``.
    """
    state = _state(ctx)
    try:
        if attempt is None:
            attempt, _resolved_cap = _resolve_attempt_cap(state)
        else:
            with contextlib.suppress(TypeError, ValueError):
                attempt = int(attempt or 0)
        if max_attempts is None:
            _, max_attempts = _resolve_attempt_cap(state)
        else:
            with contextlib.suppress(TypeError, ValueError):
                max_attempts = int(max_attempts or 10)
        max_attempts = max(1, int(max_attempts or 10))

        experiment_history = state.get("experiment_history")
        if not isinstance(experiment_history, list):
            experiment_history = []
        rejected_history = state.get("rejected_history")
        if not isinstance(rejected_history, list):
            rejected_history = []

        # Diagnosis arrival — bookkeeping only (no attempt change, no
        # experiment/rejected appends: the screen verdict was already
        # recorded once by screen_candidate before diagnosis ran).
        incoming_diag = _coerce_diagnosis(node_input)
        if incoming_diag is not None:
            diag_hist = state.get("diagnosis_history")
            if not isinstance(diag_hist, list):
                diag_hist = []
            dump = incoming_diag.model_dump()
            diag_hist.append(dump)
            with contextlib.suppress(Exception):
                state["diagnosis_history"] = diag_hist
                state["diagnosis_output"] = dump
            # Flake counter: consecutive retry_same allows one identical retry.
            try:
                count = int(state.get("retry_same_count", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            with contextlib.suppress(Exception):
                if _correction_str(incoming_diag) == "retry_same":
                    state["retry_same_count"] = count + 1
                else:
                    state["retry_same_count"] = 0
            _sync_prompt_constraints(state)
            _refresh_memory(state)
        # Classify the input for the status label only — no appends here.
        verdict: Any = node_input
        if isinstance(verdict, dict):
            vtype = str(verdict.get("type", verdict.get("kind", ""))).lower()
            if "reason" in verdict and "status" not in verdict:
                vtype = "rejection"
            elif "status" in verdict:
                vtype = "screen"
            elif _coerce_diagnosis(verdict) is not None:
                vtype = "screen"
        elif isinstance(verdict, CompileRejection):
            vtype = "rejection"
        elif isinstance(verdict, ScreenVerdict):
            vtype = "screen"
        else:
            vtype = getattr(verdict, "type", "") or ""

        if incoming_diag is not None:
            status_label = f"diagnosed:{_correction_str(incoming_diag)}"
        elif vtype == "rejection":
            if isinstance(verdict, CompileRejection):
                name = verdict.design_name or exp_name
            elif isinstance(verdict, dict):
                name = str(verdict.get("design_name", exp_name) or exp_name)
            else:
                name = exp_name
            status_label = f"rejected:{name}"
        else:
            if isinstance(verdict, ScreenVerdict):
                status = verdict.status
                name = exp_name or "experiment"
            elif isinstance(verdict, dict):
                status = str(verdict.get("status", "FAIL"))
                name = exp_name or str(
                    verdict.get("exp_name", verdict.get("arm", "experiment"))
                )
            else:
                status = "FAIL"
                name = exp_name or "experiment"
            status_label = f"screened:{name}:{status}"
        last_failure = list(rejected_history)

        route = "retry"
        reason = ""
        gate_info: dict[str, Any] = {}
        latest = _latest_diagnosis(state)
        if latest is not None and _correction_str(latest) == "stop":
            # Two-score STOP/NEXT gate: a stop halts only when the diagnosis
            # confidence and the win probability back the stated reason.
            # A winner-claim needs strong stats (p_win >= stop_stat_win_min);
            # a futility-claim needs no winning evidence
            # (p_win <= futility_stat_max, or no usable samples at all).
            # Disagreement routes retry so more evidence is gathered; the
            # backstops below (confident win, retry_same, attempt cap) still
            # apply afterwards.
            diag_conf = _diag_confidence(latest)
            stop_reason = _stop_reason_str(latest)
            diag_min, win_min, fut_max = _gate_thresholds(state)
            stat_p, _stat_found = _best_confirmed_p_win(state, node_input)
            gate = "disagree"
            agreed = False
            if diag_conf >= diag_min:
                if stop_reason == "winner":
                    if stat_p is not None and stat_p >= win_min:
                        agreed, gate = True, "stop_agree_winner"
                elif stat_p is None or stat_p <= fut_max:
                    agreed, gate = True, "stop_agree_futility"
            gate_info = {
                "diag_confidence": diag_conf,
                "stat_p_win": stat_p,
                "stop_reason": stop_reason,
                "gate": gate,
                "thresholds": {
                    "stop_diag_min": diag_min,
                    "stop_stat_win_min": win_min,
                    "futility_stat_max": fut_max,
                },
            }
            if agreed:
                route = "done"
                reason = "stopped_by_diagnosis"
            else:
                reason = "diag_stat_disagree"
        if route == "retry":
            mean, lcb, found = _latest_verdict_stats(state, node_input)
            if found and mean > 0 and lcb > _min_improvement_pct(state):
                route = "done"
                reason = "confident_win_backstop"
            else:
                try:
                    rc = int(state.get("retry_same_count", 0) or 0)
                except (TypeError, ValueError):
                    rc = 0
                if rc >= 2:
                    route = "done"
                    reason = "retry_same_exhausted"
                elif attempt >= max_attempts:
                    route = "done"
                    reason = "attempt_cap"
        with contextlib.suppress(Exception):
            ctx.route = route
        out: dict[str, Any] = {
            "attempt": attempt,
            "max_attempts": max_attempts,
            "route": route,
            "status": status_label,
            "experiment_history": list(experiment_history),
            "rejected_history": list(rejected_history),
            "last_failure": list(last_failure),
        }
        if reason:
            out["reason"] = reason
        if gate_info:
            out.update(gate_info)
        return out
    except Exception as exc:
        with contextlib.suppress(Exception):
            state["last_failure"] = [f"controller error: {exc}"]
            ctx.route = "done"
        return {
            "attempt": 1,
            "max_attempts": 1,
            "route": "done",
            "status": "controller-error",
            "experiment_history": [],
            "rejected_history": [f"controller error: {exc}"],
            "last_failure": [f"controller error: {exc}"],
        }


# ---------------------------------------------------------------------------
# 6. decision
# ---------------------------------------------------------------------------


def _row_plan_name(row: dict[str, Any]) -> Any:
    plan = row.get("plan")
    if plan is not None:
        return plan
    result = row.get("result")
    if isinstance(result, dict):
        return result.get("plan")
    return getattr(result, "plan", None)


def _plan_dump(plan: Any) -> dict[str, Any]:
    if plan is None:
        return {}
    if isinstance(plan, dict):
        return plan
    if isinstance(plan, KnobPlan):
        return plan.model_dump()
    if hasattr(plan, "model_dump"):
        try:
            dumped = plan.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    return {}


def _cand_paired(candidate: Any) -> Any:
    if isinstance(candidate, dict):
        res = candidate.get("result")
        if isinstance(res, dict):
            return res.get("paired")
        return getattr(res, "paired", None)
    return getattr(getattr(candidate, "result", None), "paired", None)


def decision(
    ctx: Any,
    all_rows: list | None = None,
    candidates: list | None = None,
    baseline_tps: list | None = None,
    min_improvement_pct: float | None = None,
    durability_profile: str | None = None,
) -> TerminalDecision:
    """Pick the terminal outcome from collected screen rows.

    Ports the strong-win / pick-winner / INCONCLUSIVE / FAIL logic from
    ``workflow`` (lines 869-974) plus the archive payload (returned as data —
    no file writes). Never returns ``None``.
    """
    state = _state(ctx)
    try:
        rows: list[dict[str, Any]] = (
            list(all_rows)
            if all_rows is not None
            else list(state.get("all_rows") or state.get("screen_rows") or [])
        )
        cands: list[dict[str, Any]] = (
            list(candidates)
            if candidates is not None
            else list(state.get("candidates") or [])
        )
        if baseline_tps is None:
            baseline_tps = state.get("baseline_tps") or []
        if min_improvement_pct is None:
            try:
                min_improvement_pct = float(
                    state.get("min_improvement_pct", 5.0) or 5.0
                )
            except (TypeError, ValueError):
                min_improvement_pct = 5.0
        if durability_profile is None:
            durability_profile = (
                str(state.get("durability_profile", "strict") or "strict").strip().lower()
            )
        experiment_history = state.get("experiment_history") or []
        experiments_run = state.get("experiments_run", len(rows))
        try:
            experiments_run = int(experiments_run)
        except (TypeError, ValueError):
            experiments_run = len(rows)
        dataset = state.get("screen_dataset") or state.get("dataset") or {}
        single_fidelity = {
            "enabled": True,
            "candidate_repetitions": state.get("candidate_repetitions", 10),
            "candidate_seconds": state.get("candidate_measurement_seconds", 10),
            "candidate_warmup_seconds": state.get("candidate_warmup_seconds", 2),
            "early_stop_min_reps": state.get("early_stop_min_reps", 4),
        }

        winner_row: dict[str, Any] | None = None
        outcome = "fail"
        for row in rows:
            try:
                row_lcb = float(row.get("lcb_pct", 0.0))
            except (TypeError, ValueError):
                row_lcb = 0.0
            if (
                str(row.get("status", "")).upper() == "PASS"
                and bool(row.get("confirmed", False))
                and row_lcb > float(min_improvement_pct)
            ):
                winner_row = row
                outcome = "apply_winner"
                break
        if winner_row is None:
            best_row = pick_winner([r for r in rows if r.get("confirmed")])
            if best_row is not None:
                winner_row = best_row
                outcome = "keep_best"
        winner_plan_raw: Any = None
        winner_stats: dict[str, float] = {}
        winner_paired: Any = None
        if winner_row is not None:
            winner_plan_raw = _row_plan_name(winner_row)
            if winner_plan_raw is None:
                winner_hash_lookup = winner_row.get("plan_hash")
                if winner_hash_lookup:
                    for cand in cands:
                        if not isinstance(cand, dict):
                            continue
                        cand_hash = cand.get("plan_hash")
                        if cand_hash is None:
                            try:
                                cand_dump = _plan_dump(cand.get("plan"))
                                cand_hash = (
                                    KnobPlan.model_validate(cand_dump).plan_hash()
                                    if cand_dump
                                    else None
                                )
                            except Exception:
                                cand_hash = None
                        if cand_hash is not None and cand_hash == winner_hash_lookup:
                            winner_plan_raw = cand.get("plan")
                            break
            for key in ("mean_delta_pct", "lcb_pct", "ucb_pct", "df"):
                try:
                    winner_stats[key] = float(winner_row.get(key, 0.0) or 0.0)
                except (TypeError, ValueError):
                    winner_stats[key] = 0.0
            winner_paired = winner_row.get("paired")
        if winner_row is None:
            ever_paired = bool(baseline_tps) or any(
                r.get("paired") is not None for r in rows
            )
            outcome = "inconclusive" if ever_paired else "fail"

        winner_dump = _plan_dump(winner_plan_raw)
        try:
            winner_hash = (
                KnobPlan.model_validate(winner_dump).plan_hash()
                if winner_dump
                else ""
            )
        except Exception:
            winner_hash = ""
        measurement_problem = bool(
            cands and any(_cand_paired(c) is None for c in cands)
        )
        archive_payload = {
            "dataset": _jsonable(dataset),
            "durability_profile": durability_profile,
            "single_fidelity": single_fidelity,
            "min_improvement_pct": float(min_improvement_pct),
            "candidates": [
                {
                    k: _jsonable(v)
                    for k, v in r.items()
                    if k not in ("plan", "result")
                }
                for r in rows
            ],
            "winner": winner_hash,
            "measurement_problem": measurement_problem,
            "winner_improvement_confident": bool(
                winner_row and winner_row.get("improvement_confident")
            ),
            "experiment_history": _jsonable(experiment_history),
            "experiments_run": experiments_run,
        }
        reasons = [
            str(r) for row in rows for r in (row.get("reasons") or [])
        ] or list(state.get("last_failure") or state.get("rejected_history") or [])
        summary = {
            "status": outcome.upper(),
            "plan_hash": winner_hash,
            "attempt_count": experiments_run,
            "reasons": reasons,
            "paired": _jsonable(winner_paired),
            "improvement_confident": archive_payload["winner_improvement_confident"],
            "dataset": _jsonable(dataset),
            "durability_profile": durability_profile,
            "stats": winner_stats,
            "archive": archive_payload,
        }
        return TerminalDecision(
            decision=outcome, winner_plan=winner_dump, summary=summary
        )
    except Exception as exc:
        return TerminalDecision(
            decision="fail",
            winner_plan={},
            summary={"status": "FAIL", "reasons": [f"decision error: {exc}"], "archive": {}},
        )


# ---------------------------------------------------------------------------
# 7. production_preflight
# ---------------------------------------------------------------------------


def _extract_winner_plan(node_input: Any, state: dict[str, Any]) -> dict[str, Any]:
    if isinstance(node_input, TerminalDecision):
        return dict(node_input.winner_plan or {})
    if isinstance(node_input, dict):
        raw = node_input.get("winner_plan", node_input.get("plan", node_input))
        if isinstance(raw, dict) and "knobs" in raw:
            return raw
        if isinstance(raw, dict):
            return raw
    if hasattr(node_input, "model_dump"):
        try:
            dumped = node_input.model_dump()
            if isinstance(dumped, dict) and isinstance(dumped.get("winner_plan"), dict):
                return dumped["winner_plan"]
        except Exception:
            pass
    raw_state = state.get("knob_plan") or {}
    if isinstance(raw_state, dict):
        return raw_state
    if hasattr(raw_state, "model_dump"):
        try:
            return raw_state.model_dump()
        except Exception:
            return {}
    return {}


def production_preflight(
    ctx: Any,
    node_input: Any,
    apply_mode: str | None = None,
    durability_profile: str | None = None,
) -> PreflightVerdict:
    """Route a terminal decision to auto / maintenance-assisted / blocked."""
    state = _state(ctx)
    try:
        if apply_mode is None:
            apply_mode = str(state.get("apply_mode", "dynamic") or "dynamic")
        if durability_profile is None:
            durability_profile = (
                str(state.get("durability_profile", "strict") or "strict").strip().lower()
            )
        plan = _extract_winner_plan(node_input, state)
        knobs = plan.get("knobs", []) if isinstance(plan, dict) else []
        if not knobs:
            return PreflightVerdict(route="blocked", reason="empty plan: nothing to apply")
        mode = str(apply_mode or "dynamic").strip().lower()
        if mode in ("maintenance-assisted", "maintenance_assisted", "manual", "persist-static"):
            return PreflightVerdict(
                route="maintenance_assisted",
                reason=f"apply_mode={mode}: manual SQL only, no production mutation",
            )
        restart_names = sorted(
            {
                str(k.get("name", ""))
                for k in knobs
                if isinstance(k, dict)
                and (
                    bool(k.get("restart_required", False))
                    or str(k.get("scope", "")).strip().lower() == "postmaster"
                )
            }
            - {""}
        )
        if restart_names:
            return PreflightVerdict(
                route="maintenance_assisted",
                reason=(
                    "restart-required knobs "
                    f"({', '.join(restart_names)}); durability={durability_profile}: "
                    "production never restarts automatically"
                ),
            )
        return PreflightVerdict(
            route="auto",
            reason=f"all {len(knobs)} knobs dynamic under durability={durability_profile}",
        )
    except Exception as exc:
        return PreflightVerdict(route="blocked", reason=f"preflight error: {exc}")


# ---------------------------------------------------------------------------
# 8. prepare_run
# ---------------------------------------------------------------------------


def prepare_run(ctx: Any) -> dict[str, Any]:
    """Resolve run configuration from state (read-only; no benchmarking).

    Ports the read-only resolution part of the legacy closed-loop tuner (budget, profile, db
    config slot, snapshot/baseline slots, loop counters) and initializes loop
    counters idempotently. Never returns ``None``.
    """
    state = _state(ctx)
    try:
        payload: dict[str, Any] = {}
        try:
            raw_budget = state.get("resource_budget") or {}
            if isinstance(raw_budget, ResourceBudget):
                budget = raw_budget
            else:
                budget = ResourceBudget.model_validate(dict(raw_budget))
            payload["resource_budget"] = budget.model_dump()
        except Exception as exc:
            payload["resource_budget"] = {}
            payload["budget_error"] = f"invalid resource_budget: {exc}"

        profile = coerce_profile(state.get("sysbench_profile"))
        payload["sysbench_profile"] = profile.model_dump()
        try:
            max_attempts = max(1, int(state.get("max_attempts", 10) or 10))
        except (TypeError, ValueError):
            max_attempts = 10
        payload["max_attempts"] = max_attempts
        payload["dry_run"] = bool(state.get("dry_run", False))
        payload["run_id"] = state.get("run_id", "") or ""
        payload["run_dir"] = state.get("run_dir", "") or ""
        payload["db_type"] = state.get("db_type", "postgres") or "postgres"
        payload["db_version"] = state.get("db_version")
        payload["database"] = (
            state.get("database")
            or state.get("db_name")
            or state.get("dbname")
            or ""
        )
        payload["benchmark_kind"] = str(
            state.get("screening_benchmark", "sysbench") or "sysbench"
        )
        payload["workload_hint"] = str(state.get("workload_hint", "") or "")
        try:
            payload["candidate_repetitions"] = max(
                2, int(state.get("candidate_repetitions", 10) or 10)
            )
        except (TypeError, ValueError):
            payload["candidate_repetitions"] = 10
        try:
            payload["candidate_measurement_seconds"] = max(
                1, int(state.get("candidate_measurement_seconds", 10) or 10)
            )
        except (TypeError, ValueError):
            payload["candidate_measurement_seconds"] = 10
        try:
            payload["candidate_warmup_seconds"] = max(
                0, int(state.get("candidate_warmup_seconds", 2) or 2)
            )
        except (TypeError, ValueError):
            payload["candidate_warmup_seconds"] = 2
        try:
            payload["early_stop_min_reps"] = max(
                2, int(state.get("early_stop_min_reps", 4) or 4)
            )
        except (TypeError, ValueError):
            payload["early_stop_min_reps"] = 4
        try:
            payload["max_set_knobs"] = max(
                1, int(state.get("max_set_knobs", 20) or 20)
            )
        except (TypeError, ValueError):
            payload["max_set_knobs"] = 20
        try:
            payload["min_improvement_pct"] = float(
                getattr(profile, "min_improvement_pct", 5.0)
            )
        except (TypeError, ValueError):
            payload["min_improvement_pct"] = 5.0
        payload["durability_profile"] = (
            str(state.get("durability_profile", "strict") or "strict").strip().lower()
        )
        # Snapshot/baseline slots (reserved keys, no side effects here).
        payload["snapshot_slot"] = state.get("snapshot_slot", "run-snapshot")
        payload["baseline_slot"] = state.get("baseline_slot", "shared-baseline")
        payload["db_config_slot"] = bool(
            state.get("db_config")
            or state.get("db_config_path")
            or state.get("config_path")
        )

        state.setdefault("attempt", 0)
        state.setdefault("validation_attempt_count", 0)
        state.setdefault("experiments_run", 0)
        state.setdefault("experiment_history", [])
        state.setdefault("rejected_history", [])
        state.setdefault("last_failure", [])
        state.setdefault("all_rows", [])
        state.setdefault("candidates", [])
        state["run_config"] = _jsonable(payload)
        # Belt-and-braces: the controller reads its cap from state, so persist
        # the resolved value explicitly (not just inside the run_config payload).
        state["max_attempts"] = max_attempts
        return payload
    except Exception as exc:
        return {"error": f"prepare_run failed: {exc}"}
