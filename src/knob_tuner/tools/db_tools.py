"""Database tuning tools: applying database knobs and running health/connectivity tests."""

import re
from typing import Any

from src.knob_tuner.contracts import ApplyMode, KnobScope

from .db_connector import DBConfig, get_connection, run_safe_query
from .knob_scope import requires_restart
from .knobs import coerce_apply_mode


def _sql_string_literal(value: Any) -> str:
    """Escape a value for use inside a single-quoted SQL string literal."""
    text = str(value)
    text = text.replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'"


def _format_knob_sql(db_type: str, knob_name: str, knob_value: Any, restart_required: bool = False) -> str:
    """Format SQL statement for applying a knob depending on the database engine.

    Args:
        db_type: Database type ('postgres', 'postgresql', 'mysql').
        knob_name: Name of the configuration knob.
        knob_value: Value to set for the knob.
        restart_required: If True, indicates a static knob requiring restart.

    Returns:
        SQL string for setting the parameter.
    """
    db_type_norm = db_type.lower()
    val_str = str(knob_value).strip()

    if db_type_norm in ("postgres", "postgresql"):
        # For Postgres, ALTER SYSTEM SET <knob> = '<value>';
        # Quotes are valid for string, byte units (e.g. '256MB'), and numbers.
        # Normalize "<number> <pg_settings-unit>" display shapes (e.g. "393216 8kB")
        # copied from the inspector output into a SET-accepted literal first.
        val_str = _normalize_display_value(val_str)
        if isinstance(knob_value, (int, float)):
            return f"ALTER SYSTEM SET {knob_name} = {knob_value};"
        elif val_str.lower() in ("on", "off", "true", "false") or val_str.isdigit():
            return f"ALTER SYSTEM SET {knob_name} = {val_str};"
        else:
            return f"ALTER SYSTEM SET {knob_name} = {_sql_string_literal(val_str)};"

    elif db_type_norm == "mysql":
        # For MySQL, SET GLOBAL <knob> = <value>; or SET PERSIST_ONLY <knob> = <value>;
        cmd = "SET PERSIST_ONLY" if restart_required else "SET GLOBAL"
        if isinstance(knob_value, (int, float)):
            return f"{cmd} {knob_name} = {knob_value};"
        elif val_str.lower() in ("on", "off", "true", "false") or val_str.isdigit():
            return f"{cmd} {knob_name} = {val_str};"
        else:
            return f"{cmd} {knob_name} = {_sql_string_literal(val_str)};"

    else:
        raise ValueError(f"Unsupported db_type for knob application: '{db_type}'")


def _coerce_scope(value: Any) -> KnobScope | None:
    """Best-effort conversion of a raw scope value to a ``KnobScope``."""
    if value is None:
        return None
    if isinstance(value, KnobScope):
        return value
    try:
        return KnobScope(str(value).strip().lower())
    except ValueError:
        return None


def resolve_knob_scope(item: dict[str, Any]) -> KnobScope:
    """Resolve the :class:`KnobScope` for a raw knob dict.

    An explicit ``scope`` key wins. When omitted, a ``restart_required=True``
    flag implies a POSTMASTER (static) knob; otherwise the scope is UNKNOWN.
    """
    scope = _coerce_scope(item.get("scope"))
    if scope is not None:
        return scope
    if item.get("restart_required"):
        return KnobScope.POSTMASTER
    return KnobScope.UNKNOWN


def _knob_requires_restart(item: dict[str, Any], scope: KnobScope) -> bool:
    """Whether the knob needs a restart (Phase 4.7: single helper)."""
    return requires_restart(item, scope=scope)


def _applies_in_mode(scope: KnobScope, mode: ApplyMode) -> bool:
    """Return True when ``scope`` should be applied under ``mode``.

    LIVE applies the full plan: reloadable knobs activate immediately while
    restart-required (postmaster/static) knobs are persisted and only activate
    on the operator's next restart. MANUAL executes nothing (it emits SQL
    only). INTERNAL knobs are never applied under any mode.
    """
    if scope == KnobScope.INTERNAL:
        return False
    if mode == ApplyMode.LIVE:
        return scope != KnobScope.INTERNAL
    if mode == ApplyMode.MANUAL:
        return True
    return False


def apply_knobs(
    knobs: list[dict[str, Any]],
    cfg: DBConfig,
    dry_run: bool = False,
    mode: ApplyMode = ApplyMode.LIVE,
) -> list[dict[str, Any]]:
    """Apply database configuration knobs to the target database.

    Args:
        knobs: List of knob specifications, where each element is a dict with
               'name' (or 'knob'), 'value' keys, and optionally 'scope' and
               'restart_required'.
        cfg: DBConfig object.
        dry_run: If True, only plan the SQL queries without executing them.
        mode: Apply semantics (NONE/LIVE/MANUAL).

    Returns:
        List of dictionaries with ``knob``, ``value``, ``status``, ``sql`` and
        ``error`` for each knob, in input order.
    """
    if not knobs:
        return []

    mode = coerce_apply_mode(mode)

    # NONE never touches the database and emits no SQL.
    if mode == ApplyMode.NONE:
        results: list[dict[str, Any]] = []
        for item in knobs:
            name = item.get("name") or item.get("knob")
            if not name:
                continue
            results.append(
                {
                    "knob": name,
                    "value": item.get("value"),
                    "status": "skipped",
                    "sql": "",
                    "error": None,
                }
            )
        return results

    # Resolve each knob once, preserving input order.
    entries: list[dict[str, Any]] = []
    for item in knobs:
        name = item.get("name") or item.get("knob")
        if not name:
            continue
        scope = resolve_knob_scope(item)
        entries.append(
            {
                "name": name,
                "value": item.get("value"),
                "scope": scope,
                "restart_required": _knob_requires_restart(item, scope),
            }
        )

    results = [{} for _ in entries]
    to_apply: list[int] = []
    for idx, entry in enumerate(entries):
        if entry["scope"] == KnobScope.INTERNAL:
            results[idx] = {
                "knob": entry["name"],
                "value": entry["value"],
                "status": "failed",
                "sql": "",
                "error": f"internal knob rejected: {entry['name']}",
            }
        elif not _applies_in_mode(entry["scope"], mode):
            results[idx] = {
                "knob": entry["name"],
                "value": entry["value"],
                "status": "skipped",
                "sql": "",
                "error": None,
            }
        else:
            to_apply.append(idx)

    if dry_run:
        for idx in to_apply:
            entry = entries[idx]
            try:
                sql = _format_knob_sql(
                    cfg.db_type,
                    entry["name"],
                    entry["value"],
                    restart_required=entry["restart_required"],
                )
                results[idx] = {
                    "knob": entry["name"],
                    "value": entry["value"],
                    "status": "dry_run",
                    "sql": sql,
                    "error": None,
                }
            except Exception as e:
                results[idx] = {
                    "knob": entry["name"],
                    "value": entry["value"],
                    "status": "failed",
                    "sql": "",
                    "error": str(e),
                }
        return results

    conn = get_connection(cfg)
    try:
        # Enable autocommit if supported to ensure ALTER SYSTEM / SET GLOBAL commit immediately
        if hasattr(conn, "autocommit"):
            try:
                conn.autocommit = True
            except Exception:
                pass

        cursor = conn.cursor()
        try:
            for idx in to_apply:
                entry = entries[idx]
                try:
                    sql = _format_knob_sql(
                        cfg.db_type,
                        entry["name"],
                        entry["value"],
                        restart_required=entry["restart_required"],
                    )
                    cursor.execute(sql)
                    if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
                        conn.commit()
                    results[idx] = {
                        "knob": entry["name"],
                        "value": entry["value"],
                        "status": "applied",
                        "sql": sql,
                        "error": None,
                    }
                except Exception as e:
                    results[idx] = {
                        "knob": entry["name"],
                        "value": entry["value"],
                        "status": "failed",
                        "sql": "",
                        "error": str(e),
                    }

            # For Postgres, reload configuration so reloadable changes take
            # effect across all sessions. Restart-required values stay pending
            # until the operator's manual restart.
            if (
                cfg.db_type.lower() in ("postgres", "postgresql")
                and mode in (ApplyMode.LIVE, ApplyMode.MANUAL)
            ):
                try:
                    cursor.execute("SELECT pg_reload_conf();")
                    if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
                        conn.commit()
                except Exception:
                    pass
        finally:
            cursor.close()
    finally:
        conn.close()

    return results


def snapshot_settings(cfg: DBConfig, names: list[str]) -> dict[str, str]:
    """Read the current values of ``names`` from the database settings catalog.

    Args:
        cfg: DBConfig object.
        names: Knob/setting names to snapshot.

    Returns:
        Mapping of requested name to its current string value. Returns an empty
        dict on any error; never raises.
    """
    if not names:
        return {}

    try:
        db_type = cfg.db_type.lower()
        if db_type in ("postgres", "postgresql"):
            rows = run_safe_query(
                cfg,
                "SELECT name, setting FROM pg_settings WHERE name = ANY(%s);",
                params=(list(names),),
            )
            snapshot: dict[str, str] = {}
            for row in rows:
                name = row.get("name")
                if name is None:
                    continue
                snapshot[str(name)] = str(row.get("setting", ""))
            return snapshot

        if db_type == "mysql":
            try:
                rows = run_safe_query(
                    cfg,
                    "SELECT VARIABLE_NAME, VARIABLE_VALUE "
                    "FROM performance_schema.global_variables;",
                )
            except Exception:
                rows = run_safe_query(cfg, "SHOW GLOBAL VARIABLES;")

            lookup: dict[str, str] = {}
            for row in rows:
                key = (
                    row.get("VARIABLE_NAME")
                    or row.get("Variable_name")
                    or row.get("variable_name")
                )
                if key is None:
                    continue
                value = (
                    row.get("VARIABLE_VALUE")
                    or row.get("Value")
                    or row.get("variable_value")
                    or ""
                )
                lookup[str(key).lower()] = str(value)

            snapshot = {}
            for name in names:
                matched = lookup.get(str(name).lower())
                if matched is not None:
                    snapshot[name] = matched
            return snapshot

        return {}
    except Exception:
        return {}


_PG_MEMORY_UNITS = {"b", "kb", "mb", "gb", "tb", "8kb", "16kb", "32kb", "64kb"}
_PG_TIME_UNITS = {"us", "ms", "s", "min", "h", "d"}

# Suffixes accepted by Postgres SET for memory GUCs (HINT: B/kB/MB/GB/TB).
_SET_MEMORY_UNITS: tuple[tuple[str, int], ...] = (
    ("GB", 1024**3),
    ("MB", 1024**2),
    ("kB", 1024),
)

_DISPLAY_VALUE_RE = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+([A-Za-z0-9]+)\s*$")


def _normalize_display_value(val_str: str) -> str:
    """Convert a ``"<number> <pg_settings-unit>"`` display shape to a SET-accepted literal.

    The inspector renders e.g. ``current_value="262144 8kB"`` style strings and
    the model copies that shape back (``"393216 8kB"``); Postgres rejects the
    embedded space (valid units are B/kB/MB/GB/TB, no space). For known memory
    units the bare number counts in those units, so scale to total bytes and
    emit the largest evenly-dividing ``GB``/``MB``/``kB`` suffix (else ``B``);
    for known time units emit integral milliseconds. Anything unrecognized is
    returned untouched — never corrupt a value we don't understand.
    """
    match = _DISPLAY_VALUE_RE.match(val_str)
    if not match:
        return val_str
    num_part, unit_part = match.groups()
    unit_lower = unit_part.lower()
    if unit_lower in _PG_MEMORY_UNITS:
        total_bytes = _parse_memory_to_bytes(num_part, unit_lower)
        if total_bytes is None or total_bytes != int(total_bytes):
            return val_str
        total = int(total_bytes)
        for suffix, size in _SET_MEMORY_UNITS:
            if total >= size and total % size == 0:
                return f"{total // size}{suffix}"
        return f"{total}B"
    if unit_lower in _PG_TIME_UNITS:
        total_ms = _parse_time_to_ms(num_part, unit_lower)
        if total_ms is None:
            return val_str
        if float(total_ms).is_integer():
            return f"{int(total_ms)}ms"
        return f"{total_ms:g}ms"
    return val_str

def _parse_memory_to_bytes(val: str, default_unit: str = "") -> float | None:
    """Parse a memory string to bytes."""
    units = {
        "b": 1,
        "kb": 1024,
        "mb": 1024**2,
        "gb": 1024**3,
        "tb": 1024**4,
        "8kb": 8192,
        "16kb": 16384,
        "32kb": 32768,
        "64kb": 65536,
    }
    val = val.lower().strip().strip("'\"").strip()
    match = re.match(r"^([\d\.]+)\s*([a-z0-9]*)$", val)
    if not match:
        return None
    
    num_part, unit_part = match.groups()
    try:
        num = float(num_part)
    except ValueError:
        return None
        
    unit_part = unit_part or default_unit.lower()
    if unit_part and unit_part in units:
        return num * units[unit_part]
    if not unit_part:
        return num
    return None

def _parse_time_to_ms(val: str, default_unit: str = "") -> float | None:
    """Parse a time string to milliseconds."""
    units = {
        "us": 0.001,
        "ms": 1,
        "s": 1000,
        "min": 60 * 1000,
        "h": 60 * 60 * 1000,
        "d": 24 * 60 * 60 * 1000,
    }
    val = val.lower().strip().strip("'\"").strip()
    match = re.match(r"^([\d\.]+)\s*([a-z]*)$", val)
    if not match:
        return None
        
    num_part, unit_part = match.groups()
    try:
        num = float(num_part)
    except ValueError:
        return None
        
    unit_part = unit_part or default_unit.lower()
    if unit_part and unit_part in units:
        return num * units[unit_part]
    if not unit_part:
        return num
    return None

def _parse_enumvals(raw: Any) -> set[str]:
    if not raw:
        return set()
    if isinstance(raw, (list, tuple)):
        return {str(x).strip().lower() for x in raw}
    raw_str = str(raw).strip()
    if raw_str.startswith("{") and raw_str.endswith("}"):
        raw_str = raw_str[1:-1]
    return {x.strip().lower() for x in raw_str.split(",") if x.strip()}

def _compare_bool(exp: str, act: str) -> bool:
    truthy = {"on", "true", "1", "yes"}
    falsy = {"off", "false", "0", "no"}
    if exp in truthy and act in truthy:
        return True
    if exp in falsy and act in falsy:
        return True
    return False

def _compare_enum(exp: str, act: str, enumvals: set[str]) -> bool:
    if exp == act:
        return True
    truthy = {"on", "true", "1", "yes"}
    falsy = {"off", "false", "0", "no"}
    
    exp_mapped = "on" if exp in truthy and "on" in enumvals else ("off" if exp in falsy and "off" in enumvals else exp)
    act_mapped = "on" if act in truthy and "on" in enumvals else ("off" if act in falsy and "off" in enumvals else act)
    
    if exp_mapped == act_mapped:
        return True
        
    if enumvals and exp_mapped in enumvals and act_mapped in enumvals:
        if (exp_mapped in falsy) != (act_mapped in falsy):
            return False
        return True
        
    return False

def _compare_numeric(exp: str, act: str, unit: str) -> bool:
    try:
        if float(exp) == float(act):
            return True
    except ValueError:
        pass

    unit_lower = unit.lower() if unit else ""
    
    if unit_lower in _PG_MEMORY_UNITS or (not unit_lower and _parse_memory_to_bytes(exp) is not None):
        exp_bytes = _parse_memory_to_bytes(exp)
        act_bytes = _parse_memory_to_bytes(act, unit)
        if exp_bytes is not None and act_bytes is not None and exp_bytes == act_bytes:
            return True
            
    if unit_lower in _PG_TIME_UNITS or (not unit_lower and _parse_time_to_ms(exp) is not None):
        exp_ms = _parse_time_to_ms(exp)
        act_ms = _parse_time_to_ms(act, unit)
        if exp_ms is not None and act_ms is not None and exp_ms == act_ms:
            return True
            
    return False

def _compare_string(exp: str, act: str) -> bool:
    if "," in exp or "," in act:
        exp_list = [x.strip().strip("'\"") for x in exp.split(",") if x.strip()]
        act_list = [x.strip().strip("'\"") for x in act.split(",") if x.strip()]
        if exp_list == act_list:
            return True
    return exp == act

def _values_are_equivalent(expected_val: Any, actual_val: Any, unit: str = "", vartype: str = "", enumvals: Any = None) -> bool:
    """Check if expected and actual values are equivalent, considering units and types."""
    exp_str = str(expected_val).strip("'\" ").lower()
    act_str = str(actual_val).strip("'\" ").lower()
    vt = vartype.lower()

    if vt == "bool":
        return _compare_bool(exp_str, act_str)
    elif vt == "enum":
        parsed_enumvals = _parse_enumvals(enumvals)
        return _compare_enum(exp_str, act_str, parsed_enumvals)
    elif vt in ("integer", "real"):
        return _compare_numeric(exp_str, act_str, unit)
    elif vt == "string":
        return _compare_string(exp_str, act_str)
    else:
        return _compare_bool(exp_str, act_str) or _compare_numeric(exp_str, act_str, unit) or _compare_string(exp_str, act_str)



_NOOP_CURRENT_KEYS = ("current_value", "setting")


def is_noop_value(entry: dict[str, Any], value: Any) -> bool:
    """True when ``value`` is value-equivalent to the knob's current effective value.

    ``entry`` is a knob-inventory record: the current setting is read from
    ``current_value`` (falling back to ``setting``) and the ``unit``,
    ``vartype`` and ``enumvals`` enrichment fields drive the comparison, so
    ``"4GB"``, ``"4096MB"`` and page counts such as ``"524288"`` (with an
    ``8kB`` unit) all compare equal. Returns ``False`` whenever the current
    value is missing or the values cannot be parsed, so an unprovable no-op
    never blocks a recommendation.
    """
    current: Any = None
    for key in _NOOP_CURRENT_KEYS:
        candidate = entry.get(key)
        if candidate not in (None, ""):
            current = candidate
            break
    if current is None:
        return False
    try:
        return _values_are_equivalent(
            value,
            current,
            unit=str(entry.get("unit") or ""),
            vartype=str(entry.get("vartype") or ""),
            enumvals=entry.get("enumvals"),
        )
    except Exception:
        return False


# ponytail: PostgreSQL knobs whose value "-1" means "auto". pg_settings.setting
# reports the resolved value, so an exact string compare would always mismatch.
_AUTO_KNOBS = {"wal_buffers"}


def verify_active_knobs(cfg: DBConfig, expected_knobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify if expected knobs are active on the database."""
    report: dict[str, Any] = {
        "status": "ok",
        "all_verified": True,
        "knobs": [],
        "error": None,
    }
    
    if not expected_knobs:
        return report

    conn = None
    try:
        conn = get_connection(cfg)
        cursor = conn.cursor()
        
        db_type = cfg.db_type.lower()
        if db_type in ("postgres", "postgresql"):
            try:
                cursor.execute("SELECT name, setting, unit, boot_val, reset_val, pending_restart, vartype, enumvals, context FROM pg_settings;")
                rows = cursor.fetchall()
            except Exception:
                # Fallback to 6-column query
                if hasattr(conn, "rollback"):
                    conn.rollback()
                cursor.execute("SELECT name, setting, unit, boot_val, reset_val, pending_restart FROM pg_settings;")
                rows = cursor.fetchall()
            
            # pg_settings is list of dicts (if dict cursor) or tuples
            settings_map = {}
            for row in rows:
                if isinstance(row, dict):
                    settings_map[row["name"]] = row
                else:
                    s_dict = {
                        "name": row[0],
                        "setting": row[1],
                        "unit": row[2],
                        "boot_val": row[3],
                        "reset_val": row[4],
                        "pending_restart": row[5] == 't' or row[5] is True
                    }
                    if len(row) >= 9:
                        s_dict["vartype"] = row[6]
                        s_dict["enumvals"] = row[7]
                        s_dict["context"] = row[8]
                    settings_map[row[0]] = s_dict
                    
            for item in expected_knobs:
                kname = item.get("name") or item.get("knob")
                kname_str = str(kname).lower()
                expected_val = str(item.get("value")).strip()
                
                # Try finding matching kname
                matched_name = None
                for name in settings_map.keys():
                    if name.lower() == kname_str:
                        matched_name = name
                        break
                        
                if not matched_name:
                    report["knobs"].append({
                        "knob": kname,
                        "expected_value": expected_val,
                        "actual_value": "",
                        "unit": "",
                        "pending_restart": False,
                        "status": "NOT_FOUND"
                    })
                    report["all_verified"] = False
                    continue
                    
                s = settings_map[matched_name]
                actual_val = s["setting"]
                unit = s.get("unit") or ""
                pending = s["pending_restart"]
                vt = s.get("vartype", "")
                evals = s.get("enumvals")
                
                # A "-1" request for an auto knob is satisfied by any resolved value.
                if expected_val == "-1" and kname_str in _AUTO_KNOBS:
                    status = "VERIFIED"
                # We can do a simplistic check: if pending_restart is True, it's PENDING_RESTART
                elif pending:
                    status = "PENDING_RESTART"
                    report["all_verified"] = False
                else:
                    if _values_are_equivalent(expected_val, actual_val, unit=unit, vartype=vt, enumvals=evals):
                        status = "VERIFIED"
                    else:
                        status = "MISMATCH"
                        report["all_verified"] = False
                         
                report["knobs"].append({
                    "knob": matched_name,
                    "expected_value": expected_val,
                    "actual_value": str(actual_val),
                    "unit": unit,
                    "pending_restart": pending,
                    "status": status
                })

        elif db_type == "mysql":
            try:
                cursor.execute("SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_variables;")
            except Exception:
                cursor.execute("SHOW GLOBAL VARIABLES;")
                
            rows = cursor.fetchall()
            settings_map = {}
            for row in rows:
                if isinstance(row, dict):
                    k = row.get("VARIABLE_NAME") or row.get("Variable_name")
                    v = row.get("VARIABLE_VALUE") or row.get("Value")
                    if k:
                        settings_map[k.lower()] = v
                else:
                    settings_map[str(row[0]).lower()] = row[1]
                    
            for item in expected_knobs:
                kname = item.get("name") or item.get("knob")
                kname_str = str(kname).lower()
                expected_val = str(item.get("value")).strip()
                
                if kname_str not in settings_map:
                    report["knobs"].append({
                        "knob": kname,
                        "expected_value": expected_val,
                        "actual_value": "",
                        "unit": "",
                        "pending_restart": False,
                        "status": "NOT_FOUND"
                    })
                    report["all_verified"] = False
                    continue
                    
                actual_val = settings_map[kname_str]
                if _values_are_equivalent(expected_val, actual_val):
                    status = "VERIFIED"
                else:
                    status = "MISMATCH"
                    report["all_verified"] = False
                    
                report["knobs"].append({
                    "knob": kname,
                    "expected_value": expected_val,
                    "actual_value": str(actual_val),
                    "unit": "",
                    "pending_restart": False,
                    "status": status
                })

        else:
            report["status"] = "error"
            report["error"] = f"Unsupported database type: {db_type}"

    except Exception as e:
        report["status"] = "error"
        report["error"] = str(e)
        report["all_verified"] = False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
                
    return report
