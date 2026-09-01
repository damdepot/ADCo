"""Tools for knob_recommender sub-agent — reading available knobs and writing selected knobs."""

import os
from pathlib import Path
from typing import Any

from google.adk.tools import ToolContext

from src.knob_tuner.sub_agents.knob_recommender.models import KnobRecommendation
from src.knob_tuner.tools.file_tools import read_json_file, write_json_file
from src.knob_tuner.tools.kb_planner import get_knob_strategies


__all__ = [
    "read_knobs_file",
    "write_selected_knobs",
    "get_knob_strategies",
]


def read_knobs_file(tool_context: ToolContext) -> str:
    """Read the extracted database configuration knobs from ``{knob_path}/knobs.json``.

    Populates ``tool_context.state['knobs_info']`` and returns a formatted summary
    of the tunable parameters.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Formatted summary string or error message.
    """
    knob_path = (
        tool_context.state.get("knob_path")
        or tool_context.state.get("target")
        or "."
    )
    knobs_file = os.path.join(knob_path, "knobs.json")

    knobs_data: list[Any] | None = None

    if os.path.isfile(knobs_file):
        try:
            content = read_json_file(knobs_file)
            if isinstance(content, list):
                knobs_data = content
                tool_context.state["knobs_info"] = knobs_data
            elif isinstance(content, dict) and "available_knobs" in content:
                knobs_data = content["available_knobs"]
                tool_context.state["knobs_info"] = knobs_data
        except Exception as e:
            return f"ERROR: failed to read knobs file '{knobs_file}': {e}"

    if knobs_data is None:
        state_knobs = tool_context.state.get("knobs_info")
        if state_knobs and isinstance(state_knobs, list):
            knobs_data = state_knobs
        else:
            return (
                f"ERROR: knobs file not found at '{knobs_file}' and knobs_info not found in state"
            )

    lines = [
        f"Read {len(knobs_data)} tunable knobs from configuration source.",
        "",
        "## Top Tunable Knobs Summary",
    ]

    sample_count = 0
    for k in knobs_data:
        if isinstance(k, dict):
            name = k.get("name", "")
            val = k.get("current_value", "")
            unit = k.get("unit", "")
            cat = k.get("category", "")
            desc = k.get("description", "")
            unit_str = f" {unit}" if unit else ""
            desc_str = f" — {desc}" if desc else ""
            lines.append(f"- **{name}**: `{val}{unit_str}` ({cat}){desc_str}")
            sample_count += 1
            if sample_count >= 25:
                lines.append(
                    f"... and {len(knobs_data) - sample_count} additional knobs."
                )
                break

    return "\n".join(lines)


def write_selected_knobs(tool_context: ToolContext) -> str:
    """Write recommended database configuration knobs to ``{knob_path}/knobs-selected.json``.

    Extracts recommended knobs from ``tool_context.state['knob_recommender_output']``
    or ``tool_context.state['selected_knobs']`` and persists them.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Status message indicating success or error.
    """
    recs: list[dict[str, Any]] = []

    output = tool_context.state.get("knob_recommender_output")
    if output:
        if hasattr(output, "recommendations"):
            for r in output.recommendations:
                if isinstance(r, KnobRecommendation):
                    recs.append(r.model_dump())
                elif isinstance(r, dict):
                    recs.append(r)
        elif isinstance(output, dict):
            raw_recs = output.get("recommendations", [])
            for r in raw_recs:
                if isinstance(r, KnobRecommendation):
                    recs.append(r.model_dump())
                elif isinstance(r, dict):
                    recs.append(r)

    if not recs:
        selected = tool_context.state.get("selected_knobs")
        if selected and isinstance(selected, list):
            for r in selected:
                if isinstance(r, KnobRecommendation):
                    recs.append(r.model_dump())
                elif isinstance(r, dict):
                    recs.append(r)

    if not recs:
        raw_recs = tool_context.state.get("recommendations")
        if raw_recs and isinstance(raw_recs, list):
            for r in raw_recs:
                if isinstance(r, KnobRecommendation):
                    recs.append(r.model_dump())
                elif isinstance(r, dict):
                    recs.append(r)

    if not recs:
        return "ERROR: no selected/recommended knobs found in state to write"

    tool_context.state["selected_knobs"] = recs

    knob_path = (
        tool_context.state.get("knob_path")
        or tool_context.state.get("target")
        or "."
    )
    out_file = os.path.join(knob_path, "knobs-selected.json")

    try:
        write_json_file(out_file, recs)
        return f"OK: wrote {len(recs)} selected knobs to {out_file}"
    except Exception as e:
        return f"ERROR: failed to write selected knobs file: {e}"
