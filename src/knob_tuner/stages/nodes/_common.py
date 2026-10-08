"""Shared leaf helpers for the knob-tuner stage nodes.

Import-safe primitives every concern module builds on: live-state access,
inventory/context resolution, the durability trust-boundary filter, JSON
coercion, history lookups, and loop-cap reads. No imports from sibling
concern modules (DAG root).
"""

from __future__ import annotations

import os
from typing import Any

from src.knob_tuner.contracts import (
    VALID_EXPERIMENT_PHASES,
    get_certify_lcb_pct,
    get_database_name,
    get_db_config_path,
    get_max_attempts,
    get_min_improvement_pct,
    get_validation_attempt,
)
from src.knob_tuner.tools.db_connector import load_db_config
from src.knob_tuner.tools.db_tools import is_noop_value
from src.knob_tuner.tools.knobs import coerce_db_config

# Phase 4.1: canonical phases live in contracts.VALID_EXPERIMENT_PHASES.
_VALID_EXPERIMENT_PHASES = VALID_EXPERIMENT_PHASES

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
    # Real ADK google.adk.sessions.state.State is NOT a dict (no keys() /
    # __iter__), so dict(state) raises and must never be attempted as a copy
    # path — return the live mapping so reads see committed values and writes
    # (state["validation_attempt_count"] = ..., state["knobs_info"] = ...) persist.
    if hasattr(state, "get") and hasattr(state, "__setitem__"):
        return state
    # Never fall back to a detached copy: a disconnected {} silently loses
    # writes (the runaway-loop bug — attempt counter unreachable) while reads
    # see empty state. Fail loudly so a broken ctx surfaces immediately.
    raise TypeError(
        f"ctx.state must be a live mutable mapping (dict or ADK State), "
        f"got {type(state).__name__}"
    )


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


def _context_map_from_knobs_info(knobs_info: Any) -> dict[str, str]:
    """Derive a ``{name: context}`` map from ``knobs_info`` entries."""
    derived: dict[str, str] = {}
    if not isinstance(knobs_info, list):
        return derived
    for entry in knobs_info:
        if isinstance(entry, dict):
            name = entry.get("name")
            context = entry.get("context", "")
        elif hasattr(entry, "model_dump"):
            try:
                dumped = entry.model_dump()
            except Exception:
                continue
            if not isinstance(dumped, dict):
                continue
            name = dumped.get("name")
            context = dumped.get("context", "")
        else:
            name = getattr(entry, "name", None)
            context = getattr(entry, "context", "")
        if name is None:
            continue
        derived[str(name)] = str(context or "")
    return derived


def _resolve_db_config_for_context(state: Any) -> Any | None:
    """Rebuild a live DB config from state without persisting secrets."""
    try:
        getter = getattr(state, "get", None)
        if not callable(getter):
            return None
        try:
            cfg = coerce_db_config(state.get("db_config"))
        except Exception:
            cfg = None
        if cfg is not None and getattr(cfg, "password", ""):
            return cfg
        path = get_db_config_path(state)
        if path and os.path.isfile(path):
            db_type = str(state.get("db_type", "postgres") or "postgres")
            db_name = get_database_name(state)
            try:
                return load_db_config(
                    path, db_type=db_type, db_override=db_name or None
                )
            except Exception:
                return None
        try:
            cfg = coerce_db_config(state.get("db_config"))
        except Exception:
            cfg = None
        if cfg is not None:
            return cfg
    except Exception:
        return None
    return None


def _validate_recommendations(
    raw_knobs: list[dict[str, Any]],
    inventory: dict[str, dict[str, Any]],
    durability_profile: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Trust-boundary filter for candidate validation.

    Phase 4.4: the strict durability allowlist is enforced ALWAYS —
    ``durability_profile`` is accepted for compatibility but never relaxes
    the gate (code, prompt, and CLI agree on one behavior).
    """
    valid: list[dict[str, Any]] = []
    rejected: list[str] = []
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


def _plan_knob_names(plan: Any) -> set[str]:
    """Return the lowercase distinct knob names in a plan dump (never throws)."""
    names: set[str] = set()
    try:
        if isinstance(plan, dict) and isinstance(plan.get("knobs"), list):
            for spec in plan["knobs"]:
                if isinstance(spec, dict) and spec.get("name"):
                    names.add(str(spec["name"]).strip().lower())
    except Exception:
        pass
    return names


def _registry_certified_hashes(state: Any) -> tuple[set[str], int]:
    """Split certified registry entries into hashed vs hashless (never throws).

    Certification uses the same rule as the winner gate: an explicit
    ``certified`` flag is trusted, otherwise ``lcb > min_improvement_pct``.
    Returns ``(non_empty_plan_hashes, hashless_count)``.
    """
    hashes: set[str] = set()
    hashless = 0
    try:
        threshold = get_min_improvement_pct(state)
        raw = state.get("winners") if hasattr(state, "get") else None
        if not isinstance(raw, list):
            return hashes, hashless
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            flag = entry.get("certified")
            if flag is not None:
                certified = bool(flag)
            else:
                try:
                    lcb = entry.get("lcb")
                    certified = bool(lcb is not None and float(lcb) > float(threshold))
                except (TypeError, ValueError):
                    certified = False
            if not certified:
                continue
            plan_hash = entry.get("plan_hash")
            text = str(plan_hash).strip() if plan_hash is not None else ""
            if text:
                hashes.add(text)
            else:
                hashless += 1
    except Exception:
        pass
    return hashes, hashless


def count_success_candidates(state: Any) -> int:
    """Count distinct LCB-clearing winner arms (unified quota progress).

    A success candidate is a screen row whose ``lcb_pct`` exceeds the current
    ``min_improvement_pct`` — the SAME definition the winner gate uses — plus
    every certified winners-registry entry (a certified entry counts toward
    the quota even before its history row is considered). Both sides feed one
    count, deduplicated by ``plan_hash`` so an arm registered AND recorded
    is never double-counted; hashless rows/entries use ``max`` for the same
    reason. Pure read of persisted state; extra controller visits cannot
    double-count. Never throws. With no registry present this is exactly the
    history count (base behavior unchanged).
    """
    threshold = get_min_improvement_pct(state)
    hist_hashes: set[str] = set()
    hashless_hist = 0
    try:
        hist = state.get("experiment_history") if hasattr(state, "get") else None
        if isinstance(hist, list):
            for entry in hist:
                if not isinstance(entry, dict):
                    continue
                raw = entry.get("lcb_pct")
                if raw is None:
                    continue
                try:
                    lcb = float(raw)
                except (TypeError, ValueError):
                    continue
                if lcb > threshold:
                    plan_hash = entry.get("plan_hash")
                    text = str(plan_hash).strip() if plan_hash is not None else ""
                    if text:
                        hist_hashes.add(text)
                    else:
                        hashless_hist += 1
    except Exception:
        return 0
    reg_hashes, hashless_reg = _registry_certified_hashes(state)
    return len(hist_hashes | reg_hashes) + max(hashless_hist, hashless_reg)


def _cleared_knob_sets(state: Any) -> list[set[str]]:
    """Return the distinct knob sets of LCB-clearing arms (from ``all_rows``).

    ``experiment_history`` rows carry only ``plan_hash`` (no knob list), so the
    plan names come from ``all_rows``/``candidates``, joined by hash. Used by
    the knob-set distinctness guard. Never throws.
    """
    threshold = get_min_improvement_pct(state)
    wanted_hashes: set[str] = set()
    try:
        hist = state.get("experiment_history") if hasattr(state, "get") else None
        if not isinstance(hist, list):
            return []
        for entry in hist:
            if not isinstance(entry, dict):
                continue
            raw = entry.get("lcb_pct")
            plan_hash = entry.get("plan_hash")
            if raw is None or not plan_hash:
                continue
            try:
                lcb = float(raw)
            except (TypeError, ValueError):
                continue
            if lcb > threshold:
                wanted_hashes.add(str(plan_hash))
    except Exception:
        return []
    if not wanted_hashes:
        return []
    by_hash: dict[str, set[str]] = {}
    try:
        for key in ("all_rows", "candidates"):
            items = state.get(key) if hasattr(state, "get") else None
            if not isinstance(items, list):
                continue
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                plan_hash = entry.get("plan_hash")
                if not plan_hash or str(plan_hash) not in wanted_hashes:
                    continue
                names = _plan_knob_names(entry.get("plan"))
                if names:
                    by_hash[str(plan_hash)] = names
    except Exception:
        return []
    seen: set[frozenset[str]] = set()
    out: list[set[str]] = []
    for names in by_hash.values():
        key = frozenset(names)
        if key and key not in seen:
            seen.add(key)
            out.append(names)
    return out


def success_knob_counts(state: Any) -> dict[str, int]:
    """Count how many LCB-clearing arms each knob appeared in (never throws).

    The exploitation signal the generator has never had: "these knobs are
    known good". A knob credited across several clearing arms ranks highest.
    Sourced from the clearing arms' knob sets (all_rows + history hashes).
    """
    counts: dict[str, int] = {}
    try:
        for names in _cleared_knob_sets(state):
            for name in names:
                counts[name] = counts.get(name, 0) + 1
    except Exception:
        return {}
    return counts


# ---------------------------------------------------------------------------
# Loop accounting (attempt counting lives in the outcome producers)
# ---------------------------------------------------------------------------


def _resolve_attempt_cap(state: Any) -> tuple[int, int]:
    """Read ``(validation_attempt_count, max_attempts)`` WITHOUT incrementing.

    R3: canonical-only (legacy ``attempt`` mirror deleted).
    """
    attempt = get_validation_attempt(state)
    max_attempts = get_max_attempts(state)
    return attempt, max(1, max_attempts)


def _min_improvement_pct(state: Any) -> float:
    """Phase 4.2: plain ``.get`` default WITHOUT ``or`` (0.0 honored)."""
    return get_min_improvement_pct(state)


def _certify_lcb_pct(state: Any) -> float:
    """Phase 4.2: plain ``.get`` default WITHOUT ``or`` (0.0 honored)."""
    return get_certify_lcb_pct(state)


def _ensure_list(state: Any, key: str) -> list:
    items = state.get(key) if hasattr(state, "get") else None
    if not isinstance(items, list):
        items = []
        # No suppression: this write is audit-trail accounting and the
        # attempt counter below is the loop's termination condition — a
        # failed write must propagate, not vanish.
        state[key] = items
    return items
