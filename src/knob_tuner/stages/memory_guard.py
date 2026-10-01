"""Memory guardrails for knob recommendations (Wave 2b).

Copied from the retired ``src.knob_tuner.sub_agents.knob_recommender.tools``
(originals removed with that package): caps memory-sized knob values to fractions of the available
RAM so stage nodes can clamp without importing agent tools.
"""

from __future__ import annotations

import re
from typing import Any

_PG_MEM_KNOBS_MAX_PCT = {
    "shared_buffers": 0.40,
    "effective_cache_size": 0.75,
    "maintenance_work_mem": 0.10,
}
_INNODB_MEM_KNOBS_MAX_PCT = {
    "innodb_buffer_pool_size": 0.75,
}
_MAX_CONNECTIONS_RAM_MB_PER_CONN = 5


def _parse_mem_value_to_bytes(value: str | float) -> float | None:
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


def parse_mem_value_to_bytes(value: Any) -> float | None:
    """Public alias for the memory-value parser."""
    if isinstance(value, (str, int, float)):
        return _parse_mem_value_to_bytes(value)
    return None


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
                    new_r[val_key] = (
                        str(max_allowed_conns)
                        if isinstance(orig_val, str)
                        else max_allowed_conns
                    )
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


def clamp_memory_knobs(
    recs: list[dict],
    memory_gb: float,
    max_connections_override: int | None = None,
) -> list[dict]:
    """Public alias for :func:`_clamp_memory_knobs`."""
    return _clamp_memory_knobs(recs, memory_gb, max_connections_override)
