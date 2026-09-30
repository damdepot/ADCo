"""Tools for knob_recommender sub-agent — reading available knobs and writing selected knobs."""

import os
import re
from typing import Any

from google.adk.tools import ToolContext

from src.knob_tuner.sub_agents.knob_recommender.models import KnobRecommendation
from src.knob_tuner.tools.file_tools import read_json_file, write_json_file
from src.knob_tuner.tools.kb_planner import get_knob_strategies


__all__ = [
    "read_knob_details",
    "write_selected_knobs",
    "get_knob_strategies",
]


_DESC_TRUNC = 160


def read_knob_details(names: str, tool_context: ToolContext) -> str:
    """Fetch full details for the requested knob names.

    Loads the knob inventory from ``{knob_path}/knobs.json`` (falling back to
    ``tool_context.state['knobs_info']``) and returns a compact formatted block,
    one line per requested knob.

    Args:
        names: Comma-separated string of knob names.
        tool_context: ADK tool execution context.

    Returns:
        Formatted details string or ``"ERROR: ..."`` message.
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
        except Exception:
            knobs_data = None

    if knobs_data is None:
        state_knobs = tool_context.state.get("knobs_info")
        if state_knobs and isinstance(state_knobs, list):
            knobs_data = state_knobs

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

    for key in ("knob_recommender_output", "selected_knobs", "recommendations"):
        value = tool_context.state.get(key)
        if not value:
            continue
        if hasattr(value, "recommendations"):
            raw_recs = value.recommendations
        elif isinstance(value, dict):
            raw_recs = value.get("recommendations", [])
        elif isinstance(value, list):
            raw_recs = value
        else:
            continue
        for r in raw_recs:
            if isinstance(r, KnobRecommendation):
                recs.append(r.model_dump())
            elif isinstance(r, dict):
                recs.append(r)
        if recs:
            break

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
