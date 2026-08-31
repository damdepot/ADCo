"""Tools for knob_checker sub-agent — apply knobs to staging, restart staging DB, test staging DB."""

import os
from typing import Any

from google.adk.tools import ToolContext

from src.knob_tuner.tools.db_connector import DBConfig, load_db_config
from src.knob_tuner.tools.db_tools import apply_knobs, test_database
from src.knob_tuner.tools.file_tools import read_json_file
from src.knob_tuner.tools.restart_tools import restart_db_by_config


def _get_staging_db_config(tool_context: ToolContext) -> DBConfig | None:
    """Extract staging database configuration from tool context state."""
    state = tool_context.state

    # 1. Direct staging_db_config in state
    if "staging_db_config" in state:
        cfg = state["staging_db_config"]
        if hasattr(cfg, "host") and hasattr(cfg, "db_type"):
            return cfg
        if isinstance(cfg, dict):
            return _dict_to_db_config(cfg, default_env="staging")

    # 2. General db_config in state
    if "db_config" in state:
        cfg = state["db_config"]
        if hasattr(cfg, "host") and hasattr(cfg, "db_type"):
            return cfg
        if isinstance(cfg, dict):
            return _dict_to_db_config(cfg, default_env="staging")

    # 3. Load from config file path if specified
    config_path = state.get("config_path") or state.get("db_config_path")
    db_type = state.get("db_type") or "postgres"
    if config_path and os.path.isfile(config_path):
        try:
            return load_db_config(config_path, env="staging", db_type=db_type)
        except Exception:
            pass

    # 4. Top-level state fields
    if "db_type" in state and ("database" in state or "dbname" in state):
        db_type = state.get("db_type", "postgres")
        default_port = 5432 if "post" in db_type.lower() else 3306
        default_user = "postgres" if "post" in db_type.lower() else "root"
        return DBConfig(
            host=state.get("host", "localhost"),
            port=int(state.get("port", default_port)),
            user=state.get("user", default_user),
            password=state.get("password", ""),
            database=state.get("database", state.get("dbname", "testdb")),
            db_type=db_type,
            env=state.get("env", "staging"),
            restart_type=state.get("restart_type", "docker"),
            restart_target=state.get("restart_target", ""),
            restart_cmd=state.get("restart_cmd", ""),
            remote_host=state.get("remote_host", ""),
            remote_user=state.get("remote_user", ""),
        )

    return None


def _dict_to_db_config(d: dict[str, Any], default_env: str = "staging") -> DBConfig:
    """Convert dictionary to DBConfig instance."""
    db_type = d.get("db_type", "postgres")
    default_port = 5432 if "post" in db_type.lower() else 3306
    default_user = "postgres" if "post" in db_type.lower() else "root"
    return DBConfig(
        host=d.get("host", "localhost"),
        port=int(d.get("port", default_port)),
        user=d.get("user", default_user),
        password=d.get("password", ""),
        database=d.get("database", d.get("dbname", "testdb")),
        db_type=db_type,
        env=d.get("env", default_env),
        restart_type=d.get("restart_type", "docker"),
        restart_target=d.get("restart_target", ""),
        restart_cmd=d.get("restart_cmd", ""),
        remote_host=d.get("remote_host", ""),
        remote_user=d.get("remote_user", ""),
    )


def _load_selected_knobs(tool_context: ToolContext) -> list[dict[str, Any]]:
    """Helper to retrieve selected knob recommendations from state or file."""
    # 1. State selected_knobs
    selected = tool_context.state.get("selected_knobs")
    if selected and isinstance(selected, list):
        knobs: list[dict[str, Any]] = []
        for item in selected:
            if hasattr(item, "model_dump"):
                d = item.model_dump()
            elif isinstance(item, dict):
                d = item
            else:
                continue
            name = d.get("knob") or d.get("name")
            val = d.get("recommended_value") or d.get("value")
            if name and val is not None:
                knobs.append({"name": name, "value": val, "restart_required": d.get("restart_required", False)})
        if knobs:
            return knobs

    # 2. State knob_recommender_output
    output = tool_context.state.get("knob_recommender_output")
    if output:
        raw_recs = []
        if hasattr(output, "recommendations"):
            raw_recs = output.recommendations
        elif isinstance(output, dict):
            raw_recs = output.get("recommendations", [])

        knobs = []
        for item in raw_recs:
            if hasattr(item, "model_dump"):
                d = item.model_dump()
            elif isinstance(item, dict):
                d = item
            else:
                continue
            name = d.get("knob") or d.get("name")
            val = d.get("recommended_value") or d.get("value")
            if name and val is not None:
                knobs.append({"name": name, "value": val, "restart_required": d.get("restart_required", False)})
        if knobs:
            return knobs

    # 3. Read from file
    knob_path = (
        tool_context.state.get("knob_path")
        or tool_context.state.get("target")
        or "."
    )
    sel_file = os.path.join(knob_path, "knobs-selected.json")
    if os.path.isfile(sel_file):
        try:
            content = read_json_file(sel_file)
            if isinstance(content, list):
                knobs = []
                for item in content:
                    if isinstance(item, dict):
                        name = item.get("knob") or item.get("name")
                        val = item.get("recommended_value") or item.get("value")
                        if name and val is not None:
                            knobs.append({"name": name, "value": val, "restart_required": item.get("restart_required", False)})
                return knobs
        except Exception:
            pass

    return []


def apply_knobs_staging(tool_context: ToolContext) -> str:
    """Apply recommended database configuration knobs to the staging database.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Status summary of applied knobs or error message.
    """
    cfg = _get_staging_db_config(tool_context)
    if not cfg:
        return "ERROR: Staging DBConfig not found in state"

    knobs = _load_selected_knobs(tool_context)
    if not knobs:
        return "ERROR: No selected knob recommendations found to apply to staging"

    try:
        results = apply_knobs(knobs, cfg, dry_run=False)
        tool_context.state["staging_applied_knobs"] = results

        applied_count = sum(1 for r in results if r.get("status") == "applied")
        failed_count = sum(1 for r in results if r.get("status") == "failed")

        lines = [
            f"Staging Knob Application Summary: {applied_count}/{len(results)} applied successfully, {failed_count} failed.",
            "",
            "## Knob Application Details",
        ]
        for r in results:
            status = r.get("status", "unknown")
            kname = r.get("knob", "")
            kval = r.get("value", "")
            err = r.get("error")
            err_str = f" (Error: {err})" if err else ""
            lines.append(f"- **{kname}** -> `{kval}`: **{status.upper()}**{err_str}")

        return "\n".join(lines)
    except Exception as e:
        return f"ERROR: Failed to apply knobs to staging database: {e}"


def restart_database_staging(tool_context: ToolContext) -> str:
    """Restart the staging database instance to apply changes and verify restart resilience.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Status message indicating success or failure of restart.
    """
    cfg = _get_staging_db_config(tool_context)
    if not cfg:
        return "ERROR: Staging DBConfig not found in state"

    try:
        ok, msg = restart_db_by_config(cfg)
        if ok:
            return f"OK: Staging database restarted successfully ({msg})"
        else:
            return f"ERROR: Staging database restart failed: {msg}"
    except Exception as e:
        return f"ERROR: Exception while restarting staging database: {e}"


def test_database_staging(tool_context: ToolContext) -> str:
    """Execute Option A database validation tests on the staging database.

    Runs connectivity check, ping, schema discovery, and CRUD lifecycle tests.
    Sets ``tool_context.state['staging_validated'] = (status == 'PASS')``.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Detailed test report string.
    """
    cfg = _get_staging_db_config(tool_context)
    if not cfg:
        tool_context.state["staging_validated"] = False
        return "ERROR: Staging DBConfig not found in state"

    try:
        report = test_database(cfg)
        tool_context.state["staging_test_results"] = report

        is_pass = (report.get("status") == "ok")
        tool_context.state["staging_validated"] = is_pass

        status_str = "PASS" if is_pass else "FAIL"
        checks = report.get("checks", {})
        details = report.get("details", {})
        err = report.get("error")

        lines = [
            f"Staging Database Option A Test Suite Result: **{status_str}**",
            "",
            "## Check Details",
            f"- **Connectivity**: {'OK' if checks.get('connectivity') else 'FAILED'}",
            f"- **Ping**: {'OK' if checks.get('ping') else 'FAILED'}",
            f"- **Table Scan**: {'OK' if checks.get('table_scan') else 'FAILED'}",
            f"- **CRUD Lifecycle**: {'OK' if checks.get('crud') else 'FAILED'}",
            f"- **Tables Discovered**: {', '.join(details.get('tables_found', [])) or 'None'}",
            f"- **CRUD Result**: {details.get('crud_result', 'not_run')}",
        ]
        if err:
            lines.append(f"- **Error Details**: {err}")

        return "\n".join(lines)
    except Exception as e:
        tool_context.state["staging_validated"] = False
        return f"ERROR: Option A test suite threw an exception: {e}"


test_database_staging.__test__ = False  # type: ignore[attr-defined]
