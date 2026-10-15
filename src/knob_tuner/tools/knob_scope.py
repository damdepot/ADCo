"""Knob scope classification helpers for PostgreSQL settings."""

from __future__ import annotations

from typing import Any

from src.knob_tuner.contracts import KnobPlan, KnobScope, KnobSpec
from src.knob_tuner.tools.db_connector import run_safe_query

_CONTEXT_TO_SCOPE: dict[str, KnobScope] = {
    "sighup": KnobScope.SIGHUP,
    "user": KnobScope.USER,
    "postmaster": KnobScope.POSTMASTER,
    "internal": KnobScope.INTERNAL,
}


def requires_restart(item: Any = None, *, scope: Any = None, restart_required: bool = False) -> bool:
    """Single helper for restart-required computation (Phase 4.7).

    Accepts a :class:`KnobSpec`, a knob dict (``scope``/``restart_required``
    keys), or explicit ``scope``/``restart_required`` kwargs. A knob needs a
    restart when its explicit flag is set or its scope is POSTMASTER.
    """
    if isinstance(item, KnobSpec):
        scope = item.scope
        restart_required = item.restart_required
    elif isinstance(item, dict):
        if "scope" in item:
            scope = item.get("scope")
        if "restart_required" in item:
            restart_required = bool(item.get("restart_required"))
    if isinstance(scope, KnobScope):
        scope_text = scope.value
    else:
        scope_text = str(scope or "").strip().lower()
    return bool(restart_required) or scope_text == KnobScope.POSTMASTER.value


def classify_scope(context: str | None) -> KnobScope:
    """Map a ``pg_settings.context`` value to a :class:`KnobScope`.

    Args:
        context: Raw context string from PostgreSQL, or None.

    Returns:
        Matching KnobScope, or UNKNOWN for anything unrecognized.
    """
    if context is None:
        return KnobScope.UNKNOWN
    return _CONTEXT_TO_SCOPE.get(str(context).strip().lower(), KnobScope.UNKNOWN)


def fetch_pg_settings_context(cfg: Any) -> dict[str, str]:
    """Fetch a ``{setting_name: context}`` map from ``pg_settings``.

    Args:
        cfg: Database configuration accepted by ``run_safe_query``.

    Returns:
        Mapping of setting name to context, or an empty dict on any error.
    """
    try:
        rows = run_safe_query(cfg, "SELECT name, context FROM pg_settings;")
    except Exception:
        return {}

    context_map: dict[str, str] = {}
    for row in rows:
        name = row.get("name")
        if name is None:
            continue
        context_map[str(name)] = str(row.get("context", ""))
    return context_map


def build_knob_plan(raw_knobs: list[dict], context_map: dict[str, str]) -> KnobPlan:
    """Convert raw knob recommendations into a validated :class:`KnobPlan`.

    Internal-scope knobs are rejected (skipped) because they cannot be set.

    Args:
        raw_knobs: Recommendations using ``name``/``knob`` and
            ``value``/``recommended_value`` keys.
        context_map: Mapping of setting name to ``pg_settings.context``.

    Returns:
        A KnobPlan containing only tunable knob specs.
    """
    lower_context = {str(key).lower(): value for key, value in context_map.items()}
    specs: list[KnobSpec] = []

    for raw in raw_knobs:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name") or raw.get("knob")
        if not name:
            continue

        scope = classify_scope(lower_context.get(str(name).lower()))
        if scope == KnobScope.INTERNAL:
            continue

        specs.append(
            KnobSpec(
                name=str(name),
                value=raw.get("value", raw.get("recommended_value")),
                scope=scope,
                restart_required=requires_restart(scope=scope),
                reasoning=str(raw.get("reasoning", "") or ""),
            )
        )

    return KnobPlan(knobs=specs)
