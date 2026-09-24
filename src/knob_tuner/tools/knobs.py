"""Pure, workflow-agnostic helpers for coercing and building knob plans."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.knob_tuner.contracts import (
    ApplyMode,
    KnobPlan,
    KnobScope,
    SysbenchProfile,
)
from src.knob_tuner.tools.db_connector import DBConfig
from src.knob_tuner.tools.knob_scope import build_knob_plan


def coerce_apply_mode(value: Any) -> ApplyMode:
    """Best-effort conversion of a raw apply-mode value to ``ApplyMode``."""
    if isinstance(value, ApplyMode):
        return value
    try:
        return ApplyMode(str(value).strip().lower())
    except (ValueError, AttributeError):
        return ApplyMode.DYNAMIC


def coerce_profile(value: Any) -> SysbenchProfile:
    """Coerce a raw profile value into a :class:`SysbenchProfile`."""
    if isinstance(value, SysbenchProfile):
        return value
    if isinstance(value, dict):
        try:
            return SysbenchProfile(**value)
        except Exception:
            return SysbenchProfile()
    return SysbenchProfile()


def coerce_db_config(value: Any) -> DBConfig | None:
    """Coerce a state value into a :class:`DBConfig`, or ``None``."""
    if value is None:
        return None
    if isinstance(value, DBConfig):
        return value
    if isinstance(value, dict):
        try:
            db_type = value.get("db_type", "postgres")
            default_port = 5432 if "post" in str(db_type).lower() else 3306
            return DBConfig(
                host=value.get("host", "localhost"),
                port=int(value.get("port", default_port)),
                user=value.get("user", ""),
                password=value.get("password", ""),
                database=(
                    value.get("database")
                    or value.get("db_name")
                    or value.get("dbname")
                    or ""
                ),
                db_type=db_type,
                env=value.get("env", "staging"),
                restart_type=value.get("restart_type", "docker"),
                restart_target=value.get("restart_target", ""),
                restart_cmd=value.get("restart_cmd", ""),
                remote_host=value.get("remote_host", ""),
                remote_user=value.get("remote_user", ""),
            )
        except Exception:
            return None
    return None


def normalize_knob(item: Any) -> dict[str, Any] | None:
    """Normalize a single knob recommendation to a plain dict."""
    if not isinstance(item, dict):
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        else:
            return None

    name = item.get("name") or item.get("knob")
    if not name:
        return None

    if item.get("value") is not None:
        value = item.get("value")
    else:
        value = item.get("recommended_value")

    return {
        "name": str(name),
        "value": value,
        "restart_required": bool(item.get("restart_required", False)),
        "reasoning": item.get("reasoning", "") or "",
    }


def extract_knob_list(value: Any) -> list[dict[str, Any]]:
    """Extract a normalized list of knob dicts from a variety of shapes."""
    if value is None:
        return []

    if not isinstance(value, (dict, list)) and hasattr(value, "recommendations"):
        value = getattr(value, "recommendations") or []
    elif not isinstance(value, (dict, list)) and hasattr(value, "model_dump"):
        dumped = value.model_dump()
        value = (
            dumped.get("recommendations")
            if isinstance(dumped, dict) and "recommendations" in dumped
            else [dumped]
        )

    if isinstance(value, dict):
        if "recommendations" in value:
            value = value.get("recommendations") or []
        elif "selected_knobs" in value:
            value = value.get("selected_knobs") or []
        elif "knobs" in value:
            value = value.get("knobs") or []
        else:
            value = [value]

    if not isinstance(value, list):
        value = [value]

    normalized = [normalize_knob(item) for item in value if item is not None]
    return [item for item in normalized if item is not None]


def dedupe_by_name(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first occurrence of each knob name, preserving order."""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        name = item.get("name")
        if name in seen:
            continue
        seen.add(name)
        result.append(item)
    return result


def load_raw_knobs(state: Any) -> list[dict[str, Any]]:
    """Load raw knob recommendations from session state or a selected-knobs file.

    Prefers ``selected_knobs`` (already memory-clamped by the recommender tool),
    then ``knob_recommender_output``, then ``{knob_path}/knobs-selected.json``.
    """
    candidates: list[dict[str, Any]] = []
    candidates.extend(extract_knob_list(state.get("selected_knobs")))
    candidates.extend(extract_knob_list(state.get("knob_recommender_output")))

    if not candidates:
        knob_path = state.get("knob_path") or state.get("target")
        if knob_path:
            path = os.path.join(str(knob_path), "knobs-selected.json")
            if os.path.isfile(path):
                try:
                    data = json.loads(Path(path).read_text(encoding="utf-8"))
                    candidates.extend(extract_knob_list(data))
                except Exception:
                    pass

    return dedupe_by_name(candidates)


def build_plan(
    raw_knobs: list[dict[str, Any]], context_map: dict[str, str]
) -> KnobPlan:
    """Build a :class:`KnobPlan` and preserve ``restart_required`` on UNKNOWN scopes."""
    plan = build_knob_plan(raw_knobs, context_map)

    raw_restart: set[str] = {
        str(raw.get("name"))
        for raw in raw_knobs
        if raw.get("name") and raw.get("restart_required")
    }
    for spec in plan.knobs:
        if spec.scope == KnobScope.UNKNOWN and spec.name in raw_restart:
            spec.restart_required = True

    return plan
