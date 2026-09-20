"""Tools for knob_recommender sub-agent — reading available knobs and writing selected knobs."""

import os
import re
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


_PG_MEM_KNOBS_MAX_PCT = {
    "shared_buffers": 0.40,
    "effective_cache_size": 0.75,
    "maintenance_work_mem": 0.10,
}
_INNODB_MEM_KNOBS_MAX_PCT = {
    "innodb_buffer_pool_size": 0.75,
}
_MAX_CONNECTIONS_RAM_MB_PER_CONN = 5


def _parse_mem_value_to_bytes(value: str | int | float) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    m = re.match(r"^([\d\.]+)\s*([a-z]*)$", s)
    if not m:
        return None
    num_str, unit = m.groups()
    try:
        num = float(num_str)
    except ValueError:
        return None

    if unit in ("", "b", "bytes"):
        return num
    elif unit in ("k", "kb"):
        return num * 1024
    elif unit in ("m", "mb"):
        return num * 1024**2
    elif unit in ("g", "gb"):
        return num * 1024**3
    elif unit in ("t", "tb"):
        return num * 1024**4
    return None


def _bytes_to_pg_string(b: float) -> str:
    b_int = int(b)
    if b_int >= 1024**3 and b_int % (1024**3) == 0:
        return f"{b_int // (1024**3)}GB"
    elif b_int >= 1024**2 and b_int % (1024**2) == 0:
        return f"{b_int // (1024**2)}MB"
    elif b_int >= 1024 and b_int % 1024 == 0:
        return f"{b_int // 1024}kB"

    if b_int >= 1024**3:
        return f"{int(b_int / 1024**3)}GB"
    elif b_int >= 1024**2:
        return f"{int(b_int / 1024**2)}MB"
    elif b_int >= 1024:
        return f"{int(b_int / 1024)}kB"
    return str(b_int)


def _clamp_memory_knobs(
    recs: list[dict],
    memory_gb: float,
    max_connections_override: int | None = None,
) -> list[dict]:
    memory_bytes = memory_gb * 1024**3
    max_pg = {k: v * memory_bytes for k, v in _PG_MEM_KNOBS_MAX_PCT.items()}
    max_innodb = {k: v * memory_bytes for k, v in _INNODB_MEM_KNOBS_MAX_PCT.items()}
    max_pg["maintenance_work_mem"] = min(max_pg["maintenance_work_mem"], 2 * 1024**3)

    max_conns = max_connections_override
    if max_conns is None:
        for r in recs:
            kname = str(r.get("knob_name", r.get("name", r.get("knob", "")))).lower()
            if kname == "max_connections":
                v = r.get("recommended_value", r.get("value"))
                try:
                    max_conns = int(v)
                except (ValueError, TypeError):
                    pass

    ram_mb = memory_gb * 1024
    max_allowed_conns = int(ram_mb / _MAX_CONNECTIONS_RAM_MB_PER_CONN)
    if max_conns is not None and max_conns > max_allowed_conns:
        max_conns = max_allowed_conns

    work_mem_limit = None
    if max_conns and max_conns > 0:
        work_mem_limit = (0.60 * memory_bytes) / max_conns

    out = []
    for r in recs:
        new_r = dict(r)
        kname = str(new_r.get("knob_name", new_r.get("name", new_r.get("knob", "")))).lower()
        val_key = "recommended_value" if "recommended_value" in new_r else "value"
        orig_val = new_r.get(val_key)

        if kname == "max_connections":
            try:
                if int(orig_val) > max_allowed_conns:
                    new_r[val_key] = str(max_allowed_conns) if isinstance(orig_val, str) else max_allowed_conns
            except (ValueError, TypeError):
                pass
        elif kname in max_pg or kname in max_innodb:
            limit = max_pg.get(kname) or max_innodb.get(kname)
            parsed = _parse_mem_value_to_bytes(orig_val)
            if parsed is not None and parsed > limit:
                new_r[val_key] = _bytes_to_pg_string(limit)
        elif kname == "work_mem" and work_mem_limit is not None:
            parsed = _parse_mem_value_to_bytes(orig_val)
            if parsed is not None and parsed > work_mem_limit:
                new_r[val_key] = _bytes_to_pg_string(work_mem_limit)
        out.append(new_r)
    return out


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

    memory_gb_raw = tool_context.state.get("memory_gb", 1.0)
    try:
        memory_gb = float(memory_gb_raw)
    except (ValueError, TypeError):
        memory_gb = 1.0

    recs = _clamp_memory_knobs(recs, memory_gb)

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
