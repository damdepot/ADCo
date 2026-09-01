"""Database tuning tools: applying database knobs and running health/connectivity tests."""

import re
from typing import Any
from .db_connector import DBConfig, get_connection


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
        if isinstance(knob_value, (int, float)):
            return f"ALTER SYSTEM SET {knob_name} = {knob_value};"
        elif val_str.lower() in ("on", "off", "true", "false") or val_str.isdigit():
            return f"ALTER SYSTEM SET {knob_name} = {val_str};"
        else:
            return f"ALTER SYSTEM SET {knob_name} = '{val_str}';"

    elif db_type_norm == "mysql":
        # For MySQL, SET GLOBAL <knob> = <value>; or SET PERSIST_ONLY <knob> = <value>;
        cmd = "SET PERSIST_ONLY" if restart_required else "SET GLOBAL"
        if isinstance(knob_value, (int, float)):
            return f"{cmd} {knob_name} = {knob_value};"
        elif val_str.lower() in ("on", "off", "true", "false") or val_str.isdigit():
            return f"{cmd} {knob_name} = {val_str};"
        else:
            return f"{cmd} {knob_name} = '{val_str}';"

    else:
        raise ValueError(f"Unsupported db_type for knob application: '{db_type}'")


def apply_knobs(
    knobs: list[dict[str, Any]], cfg: DBConfig, dry_run: bool = False
) -> list[dict[str, Any]]:
    """Apply database configuration knobs to the target database.

    Args:
        knobs: List of knob specifications, where each element is a dict with
               'name' (or 'knob'), 'value' keys, and optionally 'restart_required'.
        cfg: DBConfig object.
        dry_run: If True, only plan the SQL queries without executing them.

    Returns:
        List of dictionaries with status and details for each knob.
    """
    results: list[dict[str, Any]] = []

    if not knobs:
        return results

    if dry_run:
        for item in knobs:
            name = item.get("name") or item.get("knob")
            if not name:
                continue
            val = item.get("value")
            req_restart = item.get("restart_required", False)
            try:
                sql = _format_knob_sql(cfg.db_type, name, val, restart_required=req_restart)
                results.append(
                    {
                        "knob": name,
                        "value": val,
                        "status": "dry_run",
                        "sql": sql,
                        "error": None,
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "knob": name,
                        "value": val,
                        "status": "failed",
                        "sql": "",
                        "error": str(e),
                    }
                )
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
            for item in knobs:
                name = item.get("name") or item.get("knob")
                if not name:
                    continue
                val = item.get("value")
                req_restart = item.get("restart_required", False)
                try:
                    sql = _format_knob_sql(cfg.db_type, name, val, restart_required=req_restart)
                    cursor.execute(sql)
                    if hasattr(conn, "commit") and not getattr(conn, "autocommit", False):
                        conn.commit()
                    results.append(
                        {
                            "knob": name,
                            "value": val,
                            "status": "applied",
                            "sql": sql,
                            "error": None,
                        }
                    )
                except Exception as e:
                    results.append(
                        {
                            "knob": name,
                            "value": val,
                            "status": "failed",
                            "sql": sql if "sql" in locals() else "",
                            "error": str(e),
                        }
                    )
            
            # For Postgres, reload configuration so dynamic changes take effect across all sessions
            if cfg.db_type.lower() in ("postgres", "postgresql"):
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


def test_database(cfg: DBConfig) -> dict[str, Any]:
    """Perform health and connectivity validation tests on the database (Option A).

    Checks:
    1. Connectivity & Ping (SELECT 1)
    2. Table scan (information_schema query)
    3. Basic CRUD lifecycle test on a temporary test table

    Args:
        cfg: DBConfig object.

    Returns:
        Dictionary summarizing check results, status ('ok' | 'error'), and details.
    """
    report: dict[str, Any] = {
        "status": "error",
        "checks": {
            "connectivity": False,
            "ping": False,
            "table_scan": False,
            "crud": False,
        },
        "details": {
            "tables_found": [],
            "crud_result": "not_run",
        },
        "error": None,
    }

    test_table = "_adco_health_check"

    conn = None
    try:
        # 1. Connectivity Check
        conn = get_connection(cfg)
        report["checks"]["connectivity"] = True

        cursor = conn.cursor()
        try:
            # 1b. Ping Check
            cursor.execute("SELECT 1 AS ping;")
            row = cursor.fetchone()
            if row is not None:
                report["checks"]["ping"] = True

            # 2. Table Scan Check
            schema_sql = (
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'performance_schema', 'sys', 'mysql') "
                "LIMIT 5;"
            )
            cursor.execute(schema_sql)
            tables = cursor.fetchall()
            found_tables = []
            for t in tables:
                if isinstance(t, dict):
                    found_tables.append(t.get("table_name") or t.get("TABLE_NAME"))
                elif isinstance(t, (tuple, list)):
                    found_tables.append(t[0])
                else:
                    found_tables.append(str(t))
            report["checks"]["table_scan"] = True
            report["details"]["tables_found"] = found_tables

            # 3. CRUD Lifecycle Test
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {test_table} (id INT PRIMARY KEY, val VARCHAR(64));"
            )
            cursor.execute(
                f"INSERT INTO {test_table} (id, val) VALUES (999, 'health_test');"
            )
            cursor.execute(f"SELECT val FROM {test_table} WHERE id = 999;")
            fetched = cursor.fetchone()
            val_match = False
            if fetched:
                if isinstance(fetched, dict):
                    val_match = fetched.get("val") == "health_test"
                elif isinstance(fetched, (tuple, list)):
                    val_match = fetched[0] == "health_test"

            if not val_match:
                raise RuntimeError("CRUD test failed: inserted value did not match")

            cursor.execute(
                f"UPDATE {test_table} SET val = 'health_updated' WHERE id = 999;"
            )
            cursor.execute(f"DELETE FROM {test_table} WHERE id = 999;")
            cursor.execute(f"DROP TABLE IF EXISTS {test_table};")
            if hasattr(conn, "commit"):
                conn.commit()

            report["checks"]["crud"] = True
            report["details"]["crud_result"] = "passed"
            report["status"] = "ok"

        except Exception as e:
            report["error"] = str(e)
            # Try to cleanup test table if created
            try:
                cursor.execute(f"DROP TABLE IF EXISTS {test_table};")
                if hasattr(conn, "commit"):
                    conn.commit()
            except Exception:
                pass
        finally:
            cursor.close()

    except Exception as e:
        report["error"] = str(e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return report


test_database.__test__ = False  # type: ignore[attr-defined]


test_database.__test__ = False  # type: ignore[attr-defined]


_PG_MEMORY_UNITS = {"b", "kb", "mb", "gb", "tb", "8kb", "16kb", "32kb", "64kb"}
_PG_TIME_UNITS = {"us", "ms", "s", "min", "h", "d"}

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
    val = val.lower().replace(" ", "").strip("'\"")
    match = re.match(r"^([\d\.]+)([a-z]*)$", val)
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
    val = val.lower().replace(" ", "").strip("'\"")
    match = re.match(r"^([\d\.]+)([a-z]*)$", val)
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
                
                # We can do a simplistic check: if pending_restart is True, it's PENDING_RESTART
                if pending:
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
                    if k: settings_map[k.lower()] = v
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
