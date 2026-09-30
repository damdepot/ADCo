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
    "write_experiment_protocol",
    "write_next_experiment",
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


_VALID_PHASES = ("screen", "interaction", "refinement")


def _norm_level(lvl: Any) -> dict[str, Any] | None:
    if hasattr(lvl, "knob") or hasattr(lvl, "value"):
        knob = getattr(lvl, "knob", getattr(lvl, "name", ""))
        value = getattr(lvl, "value", getattr(lvl, "recommended_value", None))
        reasoning = str(getattr(lvl, "reasoning", "") or "")
    elif isinstance(lvl, dict):
        knob = lvl.get("knob", lvl.get("name", lvl.get("knob_name", "")))
        value = lvl.get("value", lvl.get("recommended_value"))
        reasoning = str(lvl.get("reasoning", "") or "")
    else:
        return None
    if not knob or value is None:
        return None
    return {"knob": str(knob), "value": value, "reasoning": reasoning}


def write_experiment_protocol(tool_context: ToolContext) -> str:
    """Write the designed experiment protocol to ``{knob_path}/experiment-protocol.json``.

    Reads the design from ``tool_context.state['experiment_design_output']``
    (object with ``.arms``, dict with ``"arms"`` key, or list under
    ``"designs"``), normalizes arms, drops invalid-phase arms, clamps memory
    levels, and persists the protocol.

    Returns:
        Status message with per-phase arm counts.
    """
    raw = tool_context.state.get("experiment_design_output")
    arms_raw: Any = []
    if raw is None:
        return "ERROR: no experiment design found in state['experiment_design_output']"
    if hasattr(raw, "arms"):
        arms_raw = raw.arms
    elif isinstance(raw, dict):
        if isinstance(raw.get("arms"), list):
            arms_raw = raw["arms"]
        elif isinstance(raw.get("designs"), list):
            arms_raw = raw["designs"]
        elif isinstance(raw, dict) and raw.get("arms") is None and raw.get("designs") is None:
            arms_raw = []
    elif isinstance(raw, list):
        arms_raw = raw
    if hasattr(raw, "designs") and not arms_raw:
        try:
            arms_raw = raw.designs  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - best-effort attr read, fall through to error path
            arms_raw = []
    # Fallback: bare list stored under state["designs"].
    if not arms_raw:
        alt = tool_context.state.get("designs")
        if isinstance(alt, list) and alt:
            arms_raw = alt
    if not arms_raw:
        return "ERROR: no experiment design found in state['experiment_design_output']"

    valid: list[dict[str, Any]] = []
    invalid: list[str] = []
    for i, entry in enumerate(arms_raw):
        if hasattr(entry, "name") or hasattr(entry, "phase"):
            name = str(getattr(entry, "name", "") or f"arm_{i}")
            phase_raw = getattr(entry, "phase", "")
            levels_raw = getattr(entry, "levels", []) or []
            rationale = str(getattr(entry, "rationale", "") or "")
        elif isinstance(entry, dict):
            name = str(entry.get("name", f"arm_{i}"))
            phase_raw = entry.get("phase", "")
            levels_raw = entry.get("levels", []) or []
            rationale = str(entry.get("rationale", "") or "")
        else:
            invalid.append(f"arm_{i}:<non-dict>")
            continue
        phase = str(phase_raw).strip().lower()
        if phase not in _VALID_PHASES:
            invalid.append(f"{name}:{phase_raw!r}")
            continue
        levels = []
        for lvl in levels_raw:
            norm = _norm_level(lvl)
            if norm is not None:
                levels.append(norm)
        if not levels:
            invalid.append(f"{name}:empty-levels")
            continue
        valid.append({"name": name, "phase": phase, "levels": levels, "rationale": rationale})

    if not valid:
        detail = f" (invalid: {', '.join(invalid)})" if invalid else ""
        return f"ERROR: no valid arms to write{detail}"

    memory_gb_raw = tool_context.state.get("memory_gb", 1.0)
    try:
        memory_gb = float(memory_gb_raw)
    except (ValueError, TypeError):
        memory_gb = 1.0

    # Clamp globally so max_connections-aware work_mem limits apply across arms.
    flat = [{"knob": lv["knob"], "recommended_value": lv["value"]} for arm in valid for lv in arm["levels"]]
    clamped = _clamp_memory_knobs(flat, memory_gb)
    idx = 0
    for arm in valid:
        for lv in arm["levels"]:
            lv["value"] = clamped[idx].get("recommended_value", lv["value"])
            idx += 1

    tool_context.state["experiment_protocol"] = valid

    knob_path = tool_context.state.get("knob_path") or tool_context.state.get("target") or "."
    out_file = os.path.join(knob_path, "experiment-protocol.json")
    try:
        write_json_file(out_file, valid)
    except Exception as e:  # noqa: BLE001 - report any persistence failure as message
        return f"ERROR: failed to write experiment protocol file: {e}"

    s = sum(1 for a in valid if a["phase"] == "screen")
    ia = sum(1 for a in valid if a["phase"] == "interaction")
    r = sum(1 for a in valid if a["phase"] == "refinement")
    msg = f"OK: wrote {len(valid)} arms ({s} screen / {ia} interaction / {r} refinement) to {out_file}"
    if invalid:
        msg += f"; skipped invalid: {', '.join(invalid)}"
    return msg


def write_next_experiment(tool_context: ToolContext) -> str:
    """Write the single next sequential experiment to ``experiment-protocol.json``.

    Reads the proposal from ``tool_context.state['experiment_design_output']``
    (object with attributes, dict with keys, or ``{"experiment": {...}}``
    wrapper), normalizes levels, validates the phase, clamps memory levels,
    and persists the normalized experiment.
    """
    raw = tool_context.state.get("experiment_design_output")
    if raw is None:
        return "ERROR: no experiment proposal found in state['experiment_design_output']"
    # Unwrap {"experiment": {...}} envelope.
    if isinstance(raw, dict) and raw.get("experiment") is not None:
        inner = raw["experiment"]
        if isinstance(inner, dict) or hasattr(inner, "name") or hasattr(inner, "phase"):
            raw = inner
    elif hasattr(raw, "experiment"):
        inner_attr = getattr(raw, "experiment", None)
        if inner_attr is not None and (
            isinstance(inner_attr, dict)
            or hasattr(inner_attr, "name")
            or hasattr(inner_attr, "phase")
        ):
            raw = inner_attr

    if hasattr(raw, "name") or hasattr(raw, "phase"):
        name = str(getattr(raw, "name", "") or "next_experiment")
        phase_raw = getattr(raw, "phase", "")
        levels_raw = getattr(raw, "levels", []) or []
        rationale = str(getattr(raw, "rationale", "") or "")
        objective = str(getattr(raw, "objective", "") or "")
    elif isinstance(raw, dict):
        name = str(raw.get("name", "next_experiment"))
        phase_raw = raw.get("phase", "")
        levels_raw = raw.get("levels", []) or []
        rationale = str(raw.get("rationale", "") or "")
        objective = str(raw.get("objective", "") or "")
    else:
        return "ERROR: no experiment proposal found in state['experiment_design_output']"

    phase = str(phase_raw).strip().lower()
    if phase not in _VALID_PHASES:
        return f"ERROR: invalid phase {phase_raw!r}: must be one of {list(_VALID_PHASES)}"

    levels: list[dict[str, Any]] = []
    for lvl in levels_raw:
        norm = _norm_level(lvl)
        if norm is not None:
            levels.append(norm)
    if not levels:
        return "ERROR: no valid levels to write"

    memory_gb_raw = tool_context.state.get("memory_gb", 1.0)
    try:
        memory_gb = float(memory_gb_raw)
    except (ValueError, TypeError):
        memory_gb = 1.0

    # Clamp via same adapt pattern as write_experiment_protocol.
    flat = [{"knob": lv["knob"], "recommended_value": lv["value"]} for lv in levels]
    clamped = _clamp_memory_knobs(flat, memory_gb)
    for lv, cl in zip(levels, clamped):
        lv["value"] = cl.get("recommended_value", lv["value"])

    normalized = {
        "name": name,
        "phase": phase,
        "levels": levels,
        "rationale": rationale,
        "objective": objective,
    }
    tool_context.state["next_experiment"] = normalized

    knob_path = tool_context.state.get("knob_path") or tool_context.state.get("target") or "."
    out_file = os.path.join(knob_path, "experiment-protocol.json")
    try:
        write_json_file(out_file, normalized)
    except Exception as e:  # noqa: BLE001 - report persistence failure as message
        return f"ERROR: failed to write experiment protocol file: {e}"
    return f"OK: wrote experiment '{name}' ({phase}, {len(levels)} knob(s)) to {out_file}"
