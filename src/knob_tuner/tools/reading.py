"""Read-only knob inventory tool — no side effects.

Relocated (not duplicated) from the retired
``src.knob_tuner.sub_agents.knob_recommender.tools`` package: the staged
graph persists nothing from the LLM, so candidate proposals flow as
structured outputs. ``candidate_generator`` and ``diagnosis_agent``
re-export this read-only tool. ``get_knob_strategies`` lives canonically
in ``src.knob_tuner.tools.kb_planner`` and is NOT duplicated here.
"""

import os
from typing import Any

from google.adk.tools import ToolContext

from src.knob_tuner.tools.file_tools import read_json_file


__all__ = [
    "read_knob_details",
]


_DESC_TRUNC = 160


def read_knob_details(names: str, tool_context: ToolContext) -> str:
    """Fetch full details for the requested knob names.

    Loads the knob inventory from ``tool_context.state['knobs_info']`` first
    (written by ``materialize_inventory`` in the staged graph), falling back
    to ``{knob_path}/knobs.json`` for legacy callers, and returns a compact
    formatted block, one line per requested knob.

    Args:
        names: Comma-separated string of knob names.
        tool_context: ADK tool execution context.

    Returns:
        Formatted details string or ``"ERROR: ..."`` message.
    """
    knobs_data: list[Any] | None = None

    # State-first: the staged graph's materialize_inventory is the single
    # writer of the inventory; the file is only a legacy fallback.
    state_knobs = tool_context.state.get("knobs_info")
    if state_knobs and isinstance(state_knobs, list):
        knobs_data = state_knobs

    knob_path = (
        tool_context.state.get("knob_path")
        or tool_context.state.get("target")
        or "."
    )
    knobs_file = os.path.join(knob_path, "knobs.json")

    if knobs_data is None and os.path.isfile(knobs_file):
        try:
            content = read_json_file(knobs_file)
            if isinstance(content, list):
                knobs_data = content
                tool_context.state["knobs_info"] = knobs_data
            elif isinstance(content, dict) and "available_knobs" in content:
                knobs_data = content["available_knobs"]
                tool_context.state["knobs_info"] = knobs_data
        except Exception:
            knobs_data = None

    if not knobs_data:
        return (
            f"ERROR: knob inventory not found at '{knobs_file}' or in state['knobs_info']"
        )

    by_name = {}
    for k in knobs_data:
        if isinstance(k, dict):
            by_name[str(k.get("name", "")).lower()] = k

    requested = [n.strip() for n in str(names).split(",") if n.strip()]

    lines = [f"Knob details ({len(requested)} requested):"]
    missing: list[str] = []
    for want in requested:
        k = by_name.get(want.lower())
        if k is None:
            missing.append(want)
            continue
        val = k.get("current_value", "")
        unit = k.get("unit", "")
        ctx = k.get("context", "")
        vartype = k.get("vartype", "")
        enumvals = k.get("enumvals")
        min_val = k.get("min_val")
        max_val = k.get("max_val")
        pending = k.get("pending_restart")
        desc = str(k.get("description", ""))

        parts = [f"- {k.get('name', want)}: {val}{f' {unit}' if unit else ''}"]
        if ctx:
            parts.append(f"context={ctx}")
        if vartype:
            parts.append(f"vartype={vartype}")
        if enumvals:
            parts.append(f"enumvals={enumvals}")
        if min_val is not None or max_val is not None:
            parts.append(f"range={min_val}..{max_val}")
        parts.append(f"pending_restart={pending}")
        if desc:
            if len(desc) > _DESC_TRUNC:
                desc = desc[: _DESC_TRUNC - 3] + "..."
            parts.append(f"desc={desc}")
        lines.append(" | ".join(parts))

    if missing:
        lines.append(f"Missing: {', '.join(missing)}")

    lines.append(
        "NOTE: propose bare numbers or standard suffixes like 512MB/64kB, "
        "never '<num> <unit>' with a space."
    )

    return "\n".join(lines)
