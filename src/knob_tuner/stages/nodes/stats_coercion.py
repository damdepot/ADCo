"""Measurement stages: inventory, compile, screen, and the controller.

Welch-stats/verdict/coercion helpers plus the four pipeline stages that
consume them: ``materialize_inventory``, ``compile_candidate``,
``screen_candidate``, and ``confirmation_controller`` (the diagnosis-driven
router over the two-score STOP/NEXT gate). Depends on :mod:`_common`,
:mod:`diagnosis`, and :mod:`accounting`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from typing import Any

from src.knob_tuner.contracts import (
    DEFAULT_MAX_ATTEMPTS,
    KnobPlan,
    get_early_stop_min_reps,
    get_max_set_knobs,
    get_max_winners,
    get_min_improvement_pct,
    get_success_candidates,
    get_validation_attempt,
)
from src.knob_tuner.stages.memory_guard import clamp_memory_knobs
from src.knob_tuner.stages.evidence import winner_families
from src.knob_tuner.stages.models import (
    CandidateProposal,
    CompiledPlan,
    CompileRejection,
    ScreenVerdict,
)
from src.knob_tuner.sub_agents.db_inspector.models import DbInspectorOutput
from src.knob_tuner.tools.experiments import run_experiment_arms
from src.knob_tuner.tools.file_tools import write_json_file
from src.knob_tuner.tools.knob_scope import fetch_pg_settings_context
from src.knob_tuner.tools.knobs import build_plan
from src.knob_tuner.tools.stats import p_win as _welch_p_win
from src.knob_tuner.stages.nodes._common import (
    _VALID_EXPERIMENT_PHASES,
    _cleared_knob_sets,
    _context_map_from_knobs_info,
    _inventory_by_name,
    _jsonable,
    _known_plan_hashes,
    _memory_gb_from_state,
    _min_improvement_pct,
    _previous_plan_knobs,
    _resolve_attempt_cap,
    _resolve_db_config_for_context,
    _state,
    _validate_recommendations,
    count_success_candidates,
)
from src.knob_tuner.stages.nodes.accounting import (
    _commit_rejection_accounting,
    _commit_screen_accounting,
)
from src.knob_tuner.stages.nodes.diagnosis import (
    _all_diagnoses,
    _coerce_diagnosis,
    _correction_str,
    _latest_diagnosis,
    _refresh_memory,
    _sync_prompt_constraints,
)


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
                    if isinstance(cand, dict) and cand.get("paired"):
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


def _diag_confidence(diag: Any) -> float:
    """Return the diagnosis confidence in [0, 1] (never throws)."""
    try:
        return min(1.0, max(0.0, float(getattr(diag, "confidence", 0.0) or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _stop_reason_str(diag: Any) -> str:
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


def _optional_float(value: Any) -> float | None:
    """Return ``float(value)``, or ``None`` when absent/unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _verdict_fields(
    verdict: Any,
) -> tuple[str, float | None, float | None, bool, list]:
    """Return ``(status, mean, lcb, confirmed, reasons)`` (never throws).

    Absence is explicit: ``mean``/``lcb`` are ``None`` (not ``0.0``) when
    the verdict carries no measurement, and the reasons say what is missing
    instead of coming back empty — so "failed" never renders as "+0.00%".
    """
    with contextlib.suppress(Exception):
        if isinstance(verdict, ScreenVerdict):
            return (
                verdict.status,
                float(verdict.mean_delta_pct or 0.0),
                float(verdict.lcb_pct or 0.0),
                bool(verdict.confirmed),
                list(verdict.reasons or []),
            )
        if isinstance(verdict, dict):
            status = str(verdict.get("status", "FAIL"))
            mean = _optional_float(verdict.get("mean_delta_pct"))
            lcb = _optional_float(verdict.get("lcb_pct"))
            confirmed = bool(verdict.get("confirmed", False))
            reasons = [str(r) for r in (verdict.get("reasons") or [])]
            missing = [
                key
                for key in ("mean_delta_pct", "lcb_pct")
                if verdict.get(key) is None
            ]
            if missing:
                reasons = list(reasons) + [
                    f"verdict field missing: {key}" for key in missing
                ]
            return status, mean, lcb, confirmed, reasons
    return (
        "FAIL",
        None,
        None,
        False,
        [f"verdict unavailable: unexpected shape {type(verdict).__name__}"],
    )


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


def _latest_incumbent_ref(
    state: Any, node_input: Any, mean: float, lcb: float
) -> dict[str, Any]:
    """Return the leading-arm reference for the triggering verdict.

    Generic: no knob names. ``plan_hash`` resolves from the verdict-shaped
    ``node_input`` first, then ``last_screen_row``, then the tail of
    ``experiment_history``; ``""`` when no hash is carried. Never throws.
    """
    plan_hash = ""
    with contextlib.suppress(Exception):
        if isinstance(node_input, dict) and node_input.get("plan_hash"):
            plan_hash = str(node_input.get("plan_hash") or "")
        if not plan_hash:
            get = getattr(state, "get", None)
            row = get("last_screen_row") if callable(get) else None
            if isinstance(row, dict) and row.get("plan_hash"):
                plan_hash = str(row.get("plan_hash") or "")
        if not plan_hash:
            get = getattr(state, "get", None)
            hist = get("experiment_history") if callable(get) else None
            if (
                isinstance(hist, list)
                and hist
                and isinstance(hist[-1], dict)
                and hist[-1].get("plan_hash")
            ):
                plan_hash = str(hist[-1].get("plan_hash") or "")
    with contextlib.suppress(Exception):
        mean = float(mean)
    with contextlib.suppress(Exception):
        lcb = float(lcb)
    return {"plan_hash": plan_hash, "mean": mean, "lcb": lcb}


def _latest_winner_row(state: Any, node_input: Any) -> dict[str, Any]:
    """Return the triggering verdict/history row for winner registration.

    Generic: prefers a verdict-shaped ``node_input`` dict, then
    ``last_screen_row``, then the tail of ``experiment_history``. Never throws.
    """
    with contextlib.suppress(Exception):
        if isinstance(node_input, dict) and (
            "status" in node_input or "paired" in node_input
        ):
            return node_input
        get = getattr(state, "get", None)
        if callable(get):
            row = get("last_screen_row")
            if isinstance(row, dict) and row:
                return row
            hist = get("experiment_history")
            if isinstance(hist, list) and hist and isinstance(hist[-1], dict):
                return hist[-1]
    return {}


def _winner_knob_names(row: Any, state: Any) -> list[str]:
    """Resolve knob name strings for a winner entry (never throws)."""
    with contextlib.suppress(Exception):
        candidates: list[Any] = []
        if isinstance(row, dict):
            for key in ("knobs", "valid_knobs", "knob_names", "knob_list"):
                value = row.get(key)
                if isinstance(value, list) and value:
                    candidates.append(value)
            plan = row.get("plan")
            if isinstance(plan, dict) and isinstance(plan.get("knobs"), list):
                candidates.append(plan["knobs"])
            verified = row.get("verified_knobs")
            if isinstance(verified, list) and verified:
                candidates.append(verified)
        get = getattr(state, "get", None)
        if callable(get):
            for key in ("last_screen_row",):
                other = get(key)
                if isinstance(other, dict):
                    plan = other.get("plan")
                    if isinstance(plan, dict) and isinstance(
                        plan.get("knobs"), list
                    ):
                        candidates.append(plan["knobs"])
        for value in candidates:
            names: list[str] = []
            for spec in value or []:
                if isinstance(spec, str) and spec.strip():
                    names.append(spec.strip())
                elif isinstance(spec, dict):
                    for k in ("name", "knob", "knob_name"):
                        label = spec.get(k)
                        if isinstance(label, str) and label.strip():
                            names.append(label.strip())
                            break
            if names:
                seen: set[str] = set()
                return [n for n in names if not (n in seen or seen.add(n))]
    return []


def _winner_family(row: Any, state: Any) -> str:
    """Resolve the knob family/category string (never throws, else ``""``)."""
    with contextlib.suppress(Exception):
        if isinstance(row, dict):
            for key in ("family", "knob_family", "category", "knob_category"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        names = _winner_knob_names(row, state)
        if names:
            get = getattr(state, "get", None)
            info = get("knobs_info") if callable(get) else None
            if isinstance(info, list) and info:
                by_name: dict[str, str] = {}
                for entry in info:
                    if isinstance(entry, dict):
                        label = str(
                            entry.get("name", "") or ""
                        ).strip().lower()
                        cat = str(
                            entry.get("category", entry.get("family", ""))
                            or ""
                        ).strip()
                        if label and cat and label not in by_name:
                            by_name[label] = cat
                cats = {by_name[n.strip().lower()] for n in names if n.strip().lower() in by_name}
                if len(cats) == 1:
                    return next(iter(cats))
    return ""


def _latest_confirmed_pass(
    state: Any, node_input: Any
) -> tuple[float, float, bool]:
    """Return ``(mean, lcb, ok)`` for the triggering verdict (never throws).

    ``ok`` is true only when the triggering verdict is a confirmed PASS —
    the same condition the apply path uses to crown a winner — with explicit
    ``mean_delta_pct``/``lcb_pct`` measurements present. Generic: no knob
    names. ``(0.0, 0.0, False)`` otherwise.
    """
    try:
        if isinstance(node_input, ScreenVerdict):
            if str(node_input.status or "").upper() == "PASS" and bool(
                node_input.confirmed
            ):
                return (
                    float(node_input.mean_delta_pct or 0.0),
                    float(node_input.lcb_pct or 0.0),
                    True,
                )
            return 0.0, 0.0, False
        row = _latest_winner_row(state, node_input)
        if not isinstance(row, dict):
            return 0.0, 0.0, False
        if str(row.get("status", "") or "").upper() != "PASS":
            return 0.0, 0.0, False
        if not bool(row.get("confirmed", False)):
            return 0.0, 0.0, False
        mean = _optional_float(row.get("mean_delta_pct"))
        lcb = _optional_float(row.get("lcb_pct"))
        if mean is None or lcb is None:
            return 0.0, 0.0, False
        return float(mean), float(lcb), True
    except Exception:  # noqa: BLE001 - predicate lookup never throws
        return 0.0, 0.0, False


def _is_entry_certified(entry: Any, gate: float) -> bool:
    """Return whether a winner entry clears the win gate (never throws).

    Entries carrying an explicit ``certified`` flag are trusted; legacy
    entries without one derive it from ``lcb > gate``.
    """
    try:
        if isinstance(entry, dict) and entry.get("certified") is not None:
            return bool(entry.get("certified"))
        if isinstance(entry, dict):
            lcb = _optional_float(entry.get("lcb"))
            if lcb is None:
                return False
            return float(lcb) > float(gate)
    except Exception:  # noqa: BLE001, S110 - certification check never throws
        pass
    return False


def _count_certified_winners(state: Any) -> int:
    """Count registry entries clearing the win gate (never throws).

    Only certified entries satisfy the winner quota; registered-but-
    uncertified arms are reported but never stop the loop.
    """
    try:
        gate = float(_min_improvement_pct(state))
        get = getattr(state, "get", None)
        raw = get("winners") if callable(get) else None
        if not isinstance(raw, list):
            return 0
        return sum(1 for w in raw if _is_entry_certified(w, gate))
    except Exception:  # noqa: BLE001 - quota count never throws
        return 0


def _update_incumbent(
    state: Any, node_input: Any, mean: float, lcb: float
) -> dict[str, Any]:
    """Track the leading arm by ``(mean, lcb)`` lexicographic (never throws).

    Strictly better challengers replace the incumbent; anything else keeps
    the current leader. Returns the incumbent either way.
    """
    candidate = _latest_incumbent_ref(state, node_input, mean, lcb)
    incumbent: dict[str, Any] = candidate
    with contextlib.suppress(Exception):
        current = state.get("incumbent")
        if isinstance(current, dict):
            try:
                cur_key = (
                    float(current.get("mean", 0.0) or 0.0),
                    float(current.get("lcb", 0.0) or 0.0),
                )
            except (TypeError, ValueError):
                cur_key = None
            if cur_key is not None and (mean, lcb) <= cur_key:
                incumbent = {
                    "plan_hash": str(current.get("plan_hash", "") or ""),
                    "mean": cur_key[0],
                    "lcb": cur_key[1],
                }
            else:
                with contextlib.suppress(Exception):
                    state["incumbent"] = dict(candidate)
        else:
            with contextlib.suppress(Exception):
                state["incumbent"] = dict(candidate)
    return incumbent


def _leader_label(state: Any, node_input: Any, incumbent: dict[str, Any]) -> str:
    """Return the arm label for continue-screening reasons (never throws)."""
    name = ""
    with contextlib.suppress(Exception):
        name, _, _ = _describe_arm(node_input, state)
        # _describe_arm falls back to "experiment" for diagnosis inputs;
        # prefer the verdict row's arm label when generic.
        if name == "experiment":
            row = state.get("last_screen_row")
            if isinstance(row, dict):
                for key in ("arm", "exp_name", "name"):
                    label = str(row.get(key, "") or "").strip()
                    if label:
                        name = label
                        break
    with contextlib.suppress(Exception):
        return str(incumbent.get("plan_hash") or name or "leader")
    return "leader"


def _register_winner(
    state: Any, node_input: Any, mean: float, lcb: float
) -> list[dict[str, Any]]:
    """Append a winner entry for the triggering verdict (never throws).

    Entry shape: ``{plan_hash, mean, lcb, knobs, family, certified}`` where
    ``certified`` is ``lcb >`` the resolved ``min_improvement_pct``. A
    ``plan_hash`` already present is never duplicated.
    """
    try:
        get = getattr(state, "get", None)
        raw = get("winners") if callable(get) else None
        winners = list(raw) if isinstance(raw, list) else []
        winners = [w for w in winners if isinstance(w, dict)]
        ref = _latest_incumbent_ref(state, node_input, mean, lcb)
        plan_hash = str(ref.get("plan_hash", "") or "")
        if any(str(w.get("plan_hash", "") or "") == plan_hash for w in winners):
            with contextlib.suppress(Exception):
                state["winners"] = winners
            return winners
        row = _latest_winner_row(state, node_input)
        mean_f = float(ref.get("mean", mean) or 0.0)
        lcb_f = float(ref.get("lcb", lcb) or 0.0)
        try:
            certified = lcb_f > float(_min_improvement_pct(state))
        except (TypeError, ValueError):
            certified = False
        entry = {
            "plan_hash": plan_hash,
            "mean": mean_f,
            "lcb": lcb_f,
            "knobs": _winner_knob_names(row, state),
            "family": _winner_family(row, state),
            "certified": bool(certified),
        }
        winners.append(entry)
        with contextlib.suppress(Exception):
            state["winners"] = winners
        return winners
    except Exception:  # noqa: BLE001 - winner registry never throws
        return []


def _winner_stop_halts(state: Any) -> bool:
    """Return whether a winner-stop halts compile (never throws).

    Generic: a winner stop is NOT terminal by itself — the controller banks
    the agreed arm in the winners registry and keeps screening
    (register-and-continue), and the unified quota ends the loop via the
    confident-win backstop. Only the attempt cap halts compile for a winner
    stop, so post-cap proposals cannot trigger further measurement. Futility
    stops always halt (handled by the caller). Missing/unreadable state
    fails closed to halt (today's behavior).
    """
    try:
        attempt, cap = _resolve_attempt_cap(state)
        return attempt >= cap
    except Exception:  # noqa: BLE001 - halt check never throws
        return True


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


def _family_diversity_violation(
    state: Any, plan: Any, inventory: dict[str, dict[str, Any]]
) -> tuple[str, list[str]]:
    """Reject same-family-only proposals while families remain uncovered.

    Multi-winner steering: when ``state["winners"]`` is non-empty, the next
    proposal must include at least one knob outside the winners' families
    (family = inventory ``category``, no new taxonomy) — but ONLY while at
    least one inventory family remains uncovered by winners. Once every
    known inventory family is covered (or no family metadata exists at
    all), the check sunsets so the loop refines the leader instead of
    idling on compile rejections. Absent/empty winners, unresolvable winner
    families, or proposal knobs with unknown family all pass through
    silently.
    """
    try:
        covered = winner_families(state)
        if not covered:
            return "", []
        inventory_families: set[str] = set()
        for entry in (inventory or {}).values():
            if not isinstance(entry, dict):
                continue
            family = str(entry.get("category", "") or "").strip().lower()
            if family:
                inventory_families.add(family)
        if not inventory_families:
            return "", []
        uncovered = inventory_families - covered
        if not uncovered:
            return "", []
        known: set[str] = set()
        try:
            specs = plan.knobs if hasattr(plan, "knobs") else []
        except Exception:  # noqa: BLE001, S110 - bad plan shape passes through
            return "", []
        for spec in specs or []:
            try:
                name = spec.name if hasattr(spec, "name") else spec.get("name")
            except Exception:  # noqa: BLE001, S110 - bad spec passes through
                continue
            entry = (inventory or {}).get(str(name or "").strip().lower()) or {}
            family = str(entry.get("category", "") or "").strip().lower()
            if family:
                known.add(family)
        if not known or not known <= covered:
            return "", []
        detail = (
            "family-diversity violation: proposal families "
            f"{sorted(known)} all covered by winner families "
            f"{sorted(covered)} (uncovered: {sorted(uncovered)})"
            " — include >=1 knob outside them"
        )
        return detail, [detail]
    except Exception:  # noqa: BLE001 - diversity check never throws
        return "", []


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
            max_set_knobs = get_max_set_knobs(state)
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
            if not context_map:
                derived = _context_map_from_knobs_info(knobs_info)
                if derived:
                    context_map = derived
                elif not (knobs_info or []):
                    cfg = _resolve_db_config_for_context(state)
                    if cfg is not None:
                        try:
                            context_map = fetch_pg_settings_context(cfg) or {}
                        except Exception:
                            context_map = {}
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
        # Wave 2 (Phase 5.4): full enforcement — EVERY diagnosis in history
        # contributes its correction. The old if/elif chain enforced only the
        # latest diagnosis (first-match-only), silently forgetting earlier
        # corrections. Violations accumulate so one rejection names every
        # applicable correction; single-diagnosis histories behave exactly
        # as before.
        diags = _all_diagnoses(state)
        if diags:
            plan_names = [str(spec.name) for spec in plan.knobs]
            plan_lower = {n.strip().lower() for n in plan_names}
            distinct_n = len({n.strip().lower() for n in plan_names})
            last_n: int | None = None
            try:
                hist = state.get("experiment_history") or []
                if isinstance(hist, list) and hist and isinstance(hist[-1], dict):
                    last_n = int(hist[-1].get("n_knobs") or 0) or None
            except (TypeError, ValueError):
                last_n = None
            prev_map: dict[str, Any] | None = None
            violations: list[str] = []
            violation_errors: list[str] = []
            # Unified quota: a winner stop is never binding on its own. The
            # controller banks the agreed arm in the winners registry and
            # keeps screening (register-and-continue); the unified
            # certified-winner quota ends the campaign via the confident-win
            # backstop, so the stop must NOT veto the next proposal here or
            # the campaign spins on compile rejections. Only the attempt cap
            # keeps a winner stop binding (no post-cap measurement).
            # Futility stops stay binding (the campaign is genuinely over).
            for diag in diags:
                correction = _correction_str(diag)
                targets = [str(t) for t in (diag.targets or [])]
                targets_lower = {t.strip().lower() for t in targets if t.strip()}
                if correction == "stop":
                    if _stop_reason_str(diag) == "winner" and not _winner_stop_halts(
                        state
                    ):
                        # Refine-around-leader: below the attempt cap a winner
                        # stop is not terminal — skip the halt and let the
                        # proposal flow to the remaining checks (diversity,
                        # repeat-hash, adjust/shrink, inventory). Futility
                        # stops always halt exactly as before.
                        continue
                    violations.append(
                        f"stop: halted by diagnosis ({diag.rationale or 'no rationale'})"
                    )
                    violation_errors.append(
                        f"stop: {diag.rationale or 'halt requested'}"
                    )
                elif correction == "drop_knob" and targets_lower:
                    hit = sorted(plan_lower & targets_lower)
                    if hit:
                        violations.append(
                            f"drop_knob violation: proposal includes excluded knob(s) {hit}"
                        )
                        violation_errors.append(
                            f"drop_knob: {', '.join(hit)} must be excluded per diagnosis"
                        )
                elif correction == "shrink_set":
                    # Floor guard: a single-knob proposal already satisfies
                    # shrink (cannot go lower), so it measures instead of
                    # dying on an unsatisfiable "must be fewer" rejection.
                    if last_n is not None and distinct_n >= last_n and distinct_n > 1:
                        violations.append(
                            "shrink_set violation: proposal has "
                            f"{distinct_n} knobs, must be fewer than last "
                            f"attempt n_knobs={last_n}"
                        )
                        violation_errors.append(
                            f"shrink_set: {distinct_n} >= {last_n}; drop to fewer knobs"
                        )
                elif correction == "change_phase":
                    if targets:
                        want = str(targets[0]).strip().lower()
                        if phase != want:
                            violations.append(
                                "change_phase violation: proposal phase "
                                f"{phase!r} != required {want!r}"
                            )
                            violation_errors.append(
                                f"change_phase: use required phase {want!r}"
                            )
                elif correction == "adjust_value" and targets_lower:
                    if prev_map is None:
                        prev_map = _previous_plan_knobs(state)
                    new_map = {
                        str(spec.name).strip().lower(): spec.value
                        for spec in plan.knobs
                    }
                    # Satisfiability guard (enforcement time): targets absent
                    # from inventory can never be met — skip them here (the
                    # issue-time guard in diagnosis sync downgrades/filters
                    # the stored correction). A fully-unknown target list
                    # is treated as downgraded, never enforced as-is.
                    try:
                        inventory_lower = {
                            str(k).strip().lower() for k in (inventory or {})
                        }
                    except Exception:
                        inventory_lower = set()
                    known_targets = sorted(
                        t for t in targets_lower if t in inventory_lower
                    ) if inventory_lower else sorted(targets_lower)
                    if not known_targets:
                        continue
                    problems: list[str] = []
                    for target in known_targets:
                        if target not in new_map:
                            problems.append(f"{target} missing from proposal")
                        elif target in prev_map and str(
                            new_map[target]
                        ).strip().lower() == str(prev_map[target]).strip().lower():
                            problems.append(
                                f"{target} value unchanged ({new_map[target]!r})"
                            )
                    if problems:
                        violations.append(
                            "adjust_value violation: " + "; ".join(problems)
                        )
                        violation_errors.extend(
                            [f"adjust_value: {p}" for p in problems]
                        )
            if violations:
                seen: set[str] = set()
                deduped = [
                    v for v in violations if not (v in seen or seen.add(v))
                ]
                seen_e: set[str] = set()
                deduped_e = [
                    e
                    for e in violation_errors
                    if not (e in seen_e or seen_e.add(e))
                ]
                return CompileRejection(
                    reason="; ".join(deduped),
                    errors=deduped_e,
                    design_name=exp_name,
                )
        # Multi-winner step 3: family-diversity steering. Diagnosis
        # corrections above keep priority (any diagnosis violation already
        # returned); this cheap retry only fires on diagnosis-clean,
        # same-family-only proposals after a winner is registered.
        try:
            div_reason, div_errors = _family_diversity_violation(
                state, plan, inventory
            )
        except Exception:  # noqa: BLE001, S110 - diversity never blocks compile
            div_reason, div_errors = "", []
        if div_reason:
            return CompileRejection(
                reason=div_reason,
                errors=div_errors,
                design_name=exp_name,
            )
        # Wave 2: repeat guard — reject already-seen plan hashes.
        try:
            proposed_hash = plan.plan_hash()
            latest_diag = _latest_diagnosis(state)
            retry_active = (
                latest_diag is not None
                and _correction_str(latest_diag) == "retry_same"
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
        # Wave 2 / compounding campaign: distinctness guard on the knob SET
        # (values ignored). An arm whose knob set already cleared the LCB bar
        # must not be re-proposed — "10 successes" must mean 10 genuinely
        # different combinations. Value-nudged variants of a NON-clearing set
        # stay allowed; only clearing arms consume their set.
        try:
            proposed_names = frozenset(
                str(spec.name).strip().lower() for spec in plan.knobs
            )
            if proposed_names:
                for cleared in _cleared_knob_sets(state):
                    if frozenset(cleared) == proposed_names:
                        names = ", ".join(sorted(proposed_names))
                        return CompileRejection(
                            reason=(
                                f"repeat knob set: {names} already cleared the "
                                "lcb bar"
                            ),
                            errors=[f"repeat knob set: {names}"],
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
        # No suppression: rejection accounting owns the attempt bump, so a
        # failed write must propagate instead of silently losing the count.
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


def _coerce_paired(paired: Any) -> dict[str, Any] | None:
    """Return the paired-samples mapping, or ``None`` when there is none.

    ``None`` (not ``{}`` or a ``{"repr": ...}`` placeholder) marks missing
    evidence, so ``ever_paired`` in :func:`decision` no longer upgrades a
    hard FAIL into INCONCLUSIVE on the strength of a placeholder dict.
    """
    if paired is None:
        return None
    if isinstance(paired, dict):
        return paired
    if hasattr(paired, "model_dump"):
        try:
            dumped = paired.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    return None


def _fail_verdict(reason: str) -> ScreenVerdict:
    return ScreenVerdict(
        status="FAIL",
        mean_delta_pct=0.0,
        lcb_pct=0.0,
        ucb_pct=0.0,
        confirmed=False,
        improvement_confident=False,
        stopped_early=False,
        paired=None,
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
            # Phase 4.3: run_profile is threaded as a call arg; the state
            # slot (never candidate_run_profile, which was never written).
            run_profile = state.get("run_profile")
        if attempt is None:
            attempt = get_validation_attempt(state)
        if early_stop_min_reps is None:
            early_stop_min_reps = get_early_stop_min_reps(state)
        if min_improvement_pct is None:
            # Phase 4.2: plain .get WITHOUT `or` — explicit 0.0 honored.
            min_improvement_pct = get_min_improvement_pct(state)

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
            # R3: shared_baseline is canonical (baseline mirror deleted).
            cached = state.get("shared_baseline")
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
            # Phase 4.6: store plain data, never live measurement objects.
            # R3: canonical-only (baseline mirror deleted).
            baseline_dump = _jsonable(shared_baseline)
            state["shared_baseline"] = baseline_dump
            state["baseline_cache_key"] = cache_key
        else:
            # Phase 4.6: normalize any legacy live object to plain data.
            baseline_dump = _jsonable(shared_baseline)
            state["shared_baseline"] = baseline_dump
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
        # Phase 1.5: record the staged database identity so the live apply can
        # refuse a target that is not the database this plan was validated
        # against. The attestation carries it as `database_identity`.
        with contextlib.suppress(Exception):
            result_obj = row.get("result")
            attestation = None
            if isinstance(result_obj, dict):
                attestation = result_obj.get("attestation")
            elif result_obj is not None:
                attestation = getattr(result_obj, "attestation", None)
            identity = ""
            if isinstance(attestation, dict):
                identity = str(attestation.get("database_identity", "") or "")
            elif attestation is not None:
                identity = str(
                    getattr(attestation, "database_identity", "") or ""
                )
            if identity.strip():
                state["staging_database_identity"] = identity.strip()
        plan_dump = _jsonable(plan)
        rows_lite = dict(row)
        rows_lite["plan"] = plan_dump
        # Audit trail: the per-arm result carries the attestation
        # (verified_knobs, database_identity) and the attempt timings, but
        # the "result" blob is stripped below. Persist the auditable surface
        # into state + the lite row BEFORE the pop so manifest readers can
        # answer "what changed, and why".
        with contextlib.suppress(Exception):
            result_obj = rows_lite.get("result")
            attestation_dump: Any = None
            artifacts_dump: dict[str, Any] = {}
            if isinstance(result_obj, dict):
                attestation_dump = result_obj.get("attestation")
                raw_artifacts = result_obj.get("artifacts")
                if isinstance(raw_artifacts, dict):
                    artifacts_dump = raw_artifacts
            elif result_obj is not None:
                attestation_dump = getattr(result_obj, "attestation", None)
                raw_artifacts = getattr(result_obj, "artifacts", None)
                if isinstance(raw_artifacts, dict):
                    artifacts_dump = raw_artifacts
            attestation_json = _jsonable(attestation_dump)
            if isinstance(attestation_json, dict) and attestation_json:
                state["validation_attestation"] = attestation_json
                verified = attestation_json.get("verified_knobs", []) or []
                if verified:
                    rows_lite["verified_knobs"] = _jsonable(verified)
            timings_raw = (
                artifacts_dump.get("timings")
                if isinstance(artifacts_dump, dict)
                else None
            )
            if isinstance(timings_raw, dict) and timings_raw:
                timings_json = _jsonable(timings_raw)
                rows_lite["timings"] = timings_json
                state["validation_timings"] = timings_json
        rows_lite.pop("result", None)
        # Phase 4.5: read-copy-reassign — never mutate the live state list
        # in place (ADK State persistence only sees reassignment).
        all_rows = list(state.get("all_rows") or [])
        all_rows.append(_jsonable(rows_lite))
        state["all_rows"] = all_rows
        state["last_screen_row"] = _jsonable(rows_lite)
        candidates = list(state.get("candidates") or [])
        candidates.append(
            {
                "plan": plan_dump,
                "plan_hash": row.get("plan_hash"),
                "result": {"paired": _jsonable(row.get("paired"))},
                "paired": _jsonable(row.get("paired")),
                "verified_knobs": _jsonable(rows_lite.get("verified_knobs", [])),
                "timings": _jsonable(rows_lite.get("timings", {})),
            }
        )
        state["candidates"] = candidates
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
    # No suppression: outcome accounting owns the attempt bump for every
    # screen verdict, so a failed write must propagate instead of silently
    # losing the count. (_describe_arm/_verdict_fields never throw on their
    # own; only the commit below can fail.)
    name, phase, n_knobs = _describe_arm(node_input, state)
    status, mean, lcb, confirmed, reasons = _verdict_fields(verdict)
    plan_hash = ""
    timings: dict[str, Any] | None = None
    last_row = state.get("last_screen_row")
    if isinstance(last_row, dict) and last_row.get("plan_hash"):
        plan_hash = str(last_row.get("plan_hash"))
    if isinstance(last_row, dict) and isinstance(
        last_row.get("timings"), dict
    ):
        timings = last_row["timings"]
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
        timings=timings,
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
                max_attempts = int(max_attempts or DEFAULT_MAX_ATTEMPTS)
        max_attempts = max(1, int(max_attempts or DEFAULT_MAX_ATTEMPTS))

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
        # Unified winner quota (single check): the canonical target
        # (success_candidates/max_winners alias the same quota) against the
        # unified found count (LCB-clearing history arms plus certified
        # registry entries, deduplicated by plan hash). The registry FEEDS
        # the quota; there is no rival quota check.
        target = get_success_candidates(state)
        winners_found = count_success_candidates(state)
        winner_stop_continue = False
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
            # No quota downgrade here: an agreed winner always takes the
            # register-and-continue path below (quota state is reported
            # truthfully in the continue reason). The unified quota halts the
            # loop via the backstop, never by vetoing agreement.
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
                if stop_reason == "futility":
                    route = "done"
                    reason = "stopped_by_diagnosis"
                else:
                    # Winner agreement: register-and-continue (never an early
                    # stop). The agreed arm joins the winners registry
                    # (certified iff it clears the win gate, feeding the
                    # unified quota) and screening continues so further
                    # winners can accumulate; the quota halts the loop via
                    # the confident-win backstop below (or the attempt cap).
                    # The continue reason stays truthful about quota state:
                    # quota_not_met while collecting, winner_stop_continue
                    # once the quota is met but screening continues.
                    pmean, plcb, is_pass = _latest_confirmed_pass(
                        state, node_input
                    )
                    if is_pass:
                        _register_winner(state, node_input, pmean, plcb)
                        winners_found = count_success_candidates(state)
                    if attempt >= max_attempts:
                        route = "done"
                        reason = "attempt_cap"
                    else:
                        incumbent_w = (
                            _update_incumbent(state, node_input, pmean, plcb)
                            if is_pass
                            else _latest_incumbent_ref(
                                state, node_input, pmean, plcb
                            )
                        )
                        leader_w = _leader_label(state, node_input, incumbent_w)
                        route = "retry"
                        if winners_found >= target:
                            reason = (
                                "winner_stop_continue: leader "
                                f"{leader_w} mean={pmean:+.2f}% lcb={plcb:+.2f}%"
                                " - continue screening"
                            )
                        else:
                            # Quota unmet: the agreement stands (gate stays
                            # stop_agree_winner) but the stop cannot end the
                            # campaign, so screening continues.
                            reason = (
                                "quota_not_met: leader "
                                f"{leader_w} mean={pmean:+.2f}% lcb={plcb:+.2f}%"
                                " - continue screening"
                            )
                        gate_info = dict(gate_info or {})
                        gate_info["incumbent"] = dict(incumbent_w)
                    with contextlib.suppress(Exception):
                        raw = state.get("winners")
                        if isinstance(raw, list):
                            gate_info = dict(gate_info or {})
                            gate_info["winners"] = list(raw)
                    winner_stop_continue = True
            else:
                reason = "diag_stat_disagree"
        if route == "retry" and not winner_stop_continue:
            mean, lcb, found = _latest_verdict_stats(state, node_input)
            pmean, plcb, is_pass = _latest_confirmed_pass(state, node_input)
            if is_pass:
                _register_winner(state, node_input, pmean, plcb)
            # Unified quota readiness: certified registry entries already fed
            # the count above, so one check covers both history and registry.
            quota_met = count_success_candidates(state) >= target
            if found and mean > 0 and lcb > _min_improvement_pct(state):
                if quota_met:
                    route = "done"
                    reason = "confident_win_backstop"
                else:
                    # While collecting (quota unmet), bank the leading arm as
                    # the incumbent (strictly better by (mean, lcb)
                    # lexicographic, the decision's ordering). Generic: no
                    # knob names. Quota-met -> done above is untouched, and the
                    # caps below still run so collecting can always exit.
                    incumbent = _update_incumbent(state, node_input, mean, lcb)
                    leader = _leader_label(state, node_input, incumbent)
                    route = "retry"
                    reason = (
                        "collecting_success_candidates: leader "
                        f"{leader} mean={mean:+.2f}% lcb={lcb:+.2f}%"
                    )
                    gate_info = dict(gate_info or {})
                    gate_info["incumbent"] = dict(incumbent)
            elif quota_met:
                # Quota already banked but the latest verdict is not a
                # confident win: the campaign is complete (stops screening
                # past quota instead of running to the attempt cap).
                route = "done"
                reason = "quota_met"
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
                raw_winners = state.get("winners")
                if isinstance(raw_winners, list):
                    gate_info = dict(gate_info or {})
                    gate_info["winners"] = list(raw_winners)
        winners_found = count_success_candidates(state)
        gate_info["success_candidates"] = {
            "found": winners_found,
            "target": target,
            "min_improvement_pct": _min_improvement_pct(state),
        }
        # Handoff rule 1 (disagree overwrite): a retry that overrules a stop
        # must neutralize the stale stop order, or compile (full-history
        # enforcement) halts every next proposal for an overruled order.
        # Downgrade the latest stop to a satisfiable retry_same so the next
        # round measures; the downgrade rides in the output so
        # last_controller/controller_history record it. Never throws.
        stop_overruled = False
        try:
            latest_now = _latest_diagnosis(state)
            if route == "retry" and latest_now is not None and _correction_str(
                latest_now
            ) == "stop":
                try:
                    from src.knob_tuner.stages.nodes.diagnosis import (
                        _downgrade_latest as _dg_latest,
                    )
                except Exception:
                    _dg_latest = None  # type: ignore[assignment]
                if _dg_latest is not None:
                    if _dg_latest(
                        state,
                        "retry_same",
                        "controller: stop overruled by stats gate",
                    ):
                        stop_overruled = True
                if stop_overruled:
                    with contextlib.suppress(Exception):
                        _sync_prompt_constraints(state)
                    with contextlib.suppress(Exception):
                        _refresh_memory(state)
                    gate_info = dict(gate_info or {})
                    gate_info["stop_overruled"] = True
                    gate_info["overruled_correction"] = "stop"
                    gate_info["downgraded_correction"] = "retry_same"
        except Exception:  # noqa: BLE001, S110 - overwrite never blocks routing
            stop_overruled = False
        with contextlib.suppress(Exception):
            ctx.route = route
        with contextlib.suppress(Exception):
            if "winners" not in gate_info:
                raw = state.get("winners")
                if isinstance(raw, list):
                    gate_info = dict(gate_info or {})
                    gate_info["winners"] = list(raw)
        out_max_winners = 3
        with contextlib.suppress(Exception):
            out_max_winners = get_max_winners(state)
        out: dict[str, Any] = {
            # R3: canonical-only (legacy "attempt" mirror deleted).
            "validation_attempt_count": attempt,
            "max_attempts": max_attempts,
            "max_winners": out_max_winners,
            "route": route,
            "status": status_label,
            "experiment_history": list(experiment_history),
            "rejected_history": list(rejected_history),
            "last_failure": list(last_failure),
        }
        if vtype == "rejection":
            # Audit trail: the CompileRejection reason/errors must survive in
            # the controller's state projection (not just in rejected_history)
            # so manifest readers see why a proposal never ran.
            if isinstance(verdict, CompileRejection):
                out["rejection_reason"] = verdict.reason
                out["rejection_errors"] = list(verdict.errors or [])
                out["design_name"] = verdict.design_name or ""
            elif isinstance(verdict, dict):
                if verdict.get("reason"):
                    out["rejection_reason"] = str(verdict.get("reason"))
                if isinstance(verdict.get("errors"), list):
                    out["rejection_errors"] = [
                        str(e) for e in (verdict.get("errors") or [])
                    ]
                if verdict.get("design_name"):
                    out["design_name"] = str(verdict.get("design_name"))
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
            "validation_attempt_count": 1,
            "max_attempts": 1,
            "route": "done",
            "status": "controller-error",
            "experiment_history": [],
            "rejected_history": [f"controller error: {exc}"],
            "last_failure": [f"controller error: {exc}"],
        }
