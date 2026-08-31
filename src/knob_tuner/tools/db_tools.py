"""Database tuning tools: applying database knobs and running health/connectivity tests."""

from typing import Any
from .db_connector import DBConfig, get_connection


def _format_knob_sql(db_type: str, knob_name: str, knob_value: Any) -> str:
    """Format SQL statement for applying a knob depending on the database engine.

    Args:
        db_type: Database type ('postgres', 'postgresql', 'mysql').
        knob_name: Name of the configuration knob.
        knob_value: Value to set for the knob.

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
        # For MySQL, SET GLOBAL <knob> = <value>;
        if isinstance(knob_value, (int, float)):
            return f"SET GLOBAL {knob_name} = {knob_value};"
        elif val_str.lower() in ("on", "off", "true", "false") or val_str.isdigit():
            return f"SET GLOBAL {knob_name} = {val_str};"
        else:
            return f"SET GLOBAL {knob_name} = '{val_str}';"

    else:
        raise ValueError(f"Unsupported db_type for knob application: '{db_type}'")


def apply_knobs(
    knobs: list[dict[str, Any]], cfg: DBConfig, dry_run: bool = False
) -> list[dict[str, Any]]:
    """Apply database configuration knobs to the target database.

    Args:
        knobs: List of knob specifications, where each element is a dict with
               'name' (or 'knob') and 'value' keys.
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
            try:
                sql = _format_knob_sql(cfg.db_type, name, val)
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
                try:
                    sql = _format_knob_sql(cfg.db_type, name, val)
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


# Prevent pytest from treating test_database as a test function
test_database.__test__ = False  # type: ignore[attr-defined]
