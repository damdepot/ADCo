"""Tools for live_tuner sub-agent — check staging validation and apply dynamic knobs to production."""

import os
from typing import Any

from google.adk.tools import ToolContext

from src.knob_tuner.tools.db_connector import DBConfig, load_db_config
from src.knob_tuner.tools.db_tools import apply_knobs
from src.knob_tuner.tools.file_tools import read_json_file


def _get_production_db_config(tool_context: ToolContext) -> DBConfig | None:
    """Extract production database configuration from tool context state."""
    state = tool_context.state

    # 1. Direct prod_db_config or production_db_config
    if "prod_db_config" in state:
        cfg = state["prod_db_config"]
        if hasattr(cfg, "host") and hasattr(cfg, "db_type"):
            return cfg
        if isinstance(cfg, dict):
            return _dict_to_db_config(cfg, default_env="production")

    if "production_db_config" in state:
        cfg = state["production_db_config"]
        if hasattr(cfg, "host") and hasattr(cfg, "db_type"):
            return cfg
        if isinstance(cfg, dict):
            return _dict_to_db_config(cfg, default_env="production")

    # 2. Load from config file path if specified
    config_path = state.get("config_path") or state.get("db_config_path")
    db_type = state.get("db_type") or "postgres"
    if config_path and os.path.isfile(config_path):
        try:
            return load_db_config(config_path, env="production", db_type=db_type)
        except Exception:
            pass

    # 3. General db_config in state
    if "db_config" in state:
        cfg = state["db_config"]
        if hasattr(cfg, "host") and hasattr(cfg, "db_type"):
            return cfg
        if isinstance(cfg, dict):
            return _dict_to_db_config(cfg, default_env="production")

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
            env=state.get("env", "production"),
            restart_type=state.get("restart_type", "docker"),
            restart_target=state.get("restart_target", ""),
            restart_cmd=state.get("restart_cmd", ""),
            remote_host=state.get("remote_host", ""),
            remote_user=state.get("remote_user", ""),
        )

    return None


def _dict_to_db_config(d: dict[str, Any], default_env: str = "production") -> DBConfig:
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
                knobs.append(
                    {
                        "name": name,
                        "value": val,
                        "restart_required": d.get("restart_required", False),
                        "reasoning": d.get("reasoning", ""),
                    }
                )
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
                knobs.append(
                    {
                        "name": name,
                        "value": val,
                        "restart_required": d.get("restart_required", False),
                        "reasoning": d.get("reasoning", ""),
                    }
                )
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
                            knobs.append(
                                {
                                    "name": name,
                                    "value": val,
                                    "restart_required": item.get("restart_required", False),
                                    "reasoning": item.get("reasoning", ""),
                                }
                            )
                return knobs
        except Exception:
            pass

    return []


def check_staging_validation(tool_context: ToolContext) -> str:
    """Check if staging validation has passed and live tuning is permitted.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Authorization status message.
    """
    is_validated = bool(tool_context.state.get("staging_validated", False))
    if is_validated:
        return "VALIDATED: Staging validation has PASSED. Live production tuning is authorized."
    else:
        return (
            "BLOCKED: Staging validation has NOT passed (or was not run). "
            "Live production tuning is strictly blocked."
        )


def apply_knobs_production(tool_context: ToolContext) -> str:
    """Apply validated dynamic configuration knobs to the live production database.

    Strict guardrail: requires ``tool_context.state['staging_validated'] == True``.
    Never restarts production; flags static knobs for maintenance windows.

    Args:
        tool_context: ADK tool execution context.

    Returns:
        Detailed summary of live application and deferred restart-required knobs.
    """
    if not tool_context.state.get("staging_validated", False):
        return (
            "ERROR: Guardrail check failed — staging_validated is False. "
            "Knobs cannot be applied to production."
        )

    cfg = _get_production_db_config(tool_context)
    if not cfg:
        return "ERROR: Production DBConfig not found in state"

    knobs = _load_selected_knobs(tool_context)
    if not knobs:
        return "ERROR: No knob recommendations found in state or file to apply"

    dynamic_knobs = [k for k in knobs if not k.get("restart_required", False)]
    restart_required_knobs = [k for k in knobs if k.get("restart_required", False)]

    tool_context.state["prod_restart_required_knobs"] = restart_required_knobs

    applied_results: list[dict[str, Any]] = []
    if dynamic_knobs:
        try:
            applied_results = apply_knobs(dynamic_knobs, cfg, dry_run=False)
            tool_context.state["prod_applied_knobs"] = applied_results
        except Exception as e:
            return f"ERROR: Failed to execute live knob application on production: {e}"
    else:
        tool_context.state["prod_applied_knobs"] = []

    applied_count = sum(1 for r in applied_results if r.get("status") == "applied")
    failed_count = sum(1 for r in applied_results if r.get("status") == "failed")

    lines = [
        "## Production Live Tuning Report",
        f"- **Dynamic Knobs Applied Live**: {applied_count}/{len(dynamic_knobs)} (Failed: {failed_count})",
        f"- **Static / Restart-Required Knobs Deferred**: {len(restart_required_knobs)}",
        "- **Auto-Restart Status**: DISABLED (Zero Downtime Policy)",
        "",
    ]

    if applied_results:
        lines.append("### Applied Dynamic Knobs")
        for r in applied_results:
            kname = r.get("knob", "")
            kval = r.get("value", "")
            st = r.get("status", "")
            err = r.get("error")
            err_str = f" (Error: {err})" if err else ""
            lines.append(f"- **{kname}** -> `{kval}`: **{st.upper()}**{err_str}")
        lines.append("")

    if restart_required_knobs:
        lines.append("### Deferred Knobs (Requires Scheduled Maintenance Restart)")
        for rk in restart_required_knobs:
            kname = rk.get("name", "")
            kval = rk.get("value", "")
            reason = rk.get("reasoning", "")
            reason_str = f" — {reason}" if reason else ""
            lines.append(f"- **{kname}** -> `{kval}` (Static/Restart-Required){reason_str}")

    return "\n".join(lines)
