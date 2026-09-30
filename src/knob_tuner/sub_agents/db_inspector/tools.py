"""Tools for db_inspector sub-agent — schema inspection, knob extraction, file output."""

import os

from google.adk.tools import ToolContext

from src.knob_tuner.sub_agents.db_inspector.models import (
    KnobInfo,
    TableInfo,
)
from src.knob_tuner.tools.db_connector import (
    DBConfig,
    analyze_database,
    load_db_config,
    run_safe_query,
)
from src.knob_tuner.tools.file_tools import write_json_file


def _get_db_config(tool_context: ToolContext) -> DBConfig | None:
    """Retrieve a usable DBConfig from tool context state.

    Session state keeps only a redacted view of the config (no password), so
    when the in-state copy has no secret the real config is rebuilt from the
    recorded config path. A fully-populated config already in state (embedded
    callers/tests) is honored first.
    """
    state = tool_context.state

    in_state = state.get("db_config")
    has_secret = isinstance(in_state, DBConfig) or (
        isinstance(in_state, dict) and bool(in_state.get("password"))
    )
    if not has_secret:
        path = state.get("db_config_path") or state.get("config_path")
        if isinstance(path, str) and os.path.isfile(path):
            db_type = str(state.get("db_type", "postgres") or "postgres")
            db_name = str(
                state.get("database") or state.get("db_name") or state.get("dbname") or ""
            ).strip()
            try:
                return load_db_config(path, db_type=db_type, db_override=db_name or None)
            except Exception:
                pass

    if "db_config" in state:
        cfg = state["db_config"]
        if isinstance(cfg, DBConfig):
            return cfg
        if isinstance(cfg, dict):
            db_type = cfg.get("db_type", "postgres")
            default_port = 5432 if "post" in db_type.lower() else 3306
            default_user = "postgres" if "post" in db_type.lower() else "root"
            return DBConfig(
                host=cfg.get("host", "localhost"),
                port=int(cfg.get("port", default_port)),
                user=cfg.get("user", default_user),
                password=cfg.get("password", ""),
                database=cfg.get("db_name", cfg.get("database", cfg.get("dbname", "postgres"))),
                db_type=db_type,
                env=cfg.get("env", "staging"),
                restart_type=cfg.get("restart_type", "docker"),
                restart_target=cfg.get("restart_target", ""),
                restart_cmd=cfg.get("restart_cmd", ""),
                remote_host=cfg.get("remote_host", ""),
                remote_user=cfg.get("remote_user", ""),
            )
    if "db_type" in state and ("database" in state or "dbname" in state or "db_name" in state):
        db_type = state.get("db_type", "postgres")
        default_port = 5432 if "post" in db_type.lower() else 3306
        default_user = "postgres" if "post" in db_type.lower() else "root"
        return DBConfig(
            host=state.get("host", "localhost"),
            port=int(state.get("port", default_port)),
            user=state.get("user", default_user),
            password=state.get("password", ""),
            database=state.get("db_name", state.get("database", state.get("dbname", "postgres"))),
            db_type=db_type,
            env=state.get("env", "staging"),
        )
    return None


def check_schema(tool_context: ToolContext) -> str:
    """Connect to database, query schema information, and store in session state.

    Extracts tables, column types, indexes, and approximate row counts for Postgres and MySQL.
    Writes structured schema info to ``tool_context.state['schema_info']``.
    """
    cfg = _get_db_config(tool_context)
    if not cfg:
        return "ERROR: DBConfig not found in state"

    db_type = cfg.db_type.lower()
    try:
        if "post" in db_type:
            # PostgreSQL schema inspection
            ver_rows = run_safe_query(cfg, "SELECT version() AS version;")
            db_version = ver_rows[0].get("version", "") if ver_rows else ""

            # Refresh planner statistics so reltuples reflects a freshly loaded
            # dataset (a fresh bulk load leaves reltuples stale/zero). Best
            # effort: a stale estimate only sizes the screen conservatively.
            try:
                analyze_database(cfg)
            except Exception:
                pass

            table_sql = """
                SELECT
                    c.relname AS table_name,
                    COALESCE(c.reltuples::bigint, 0) AS approx_row_count
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND c.relkind = 'r'
                ORDER BY c.relname;
            """
            table_rows = run_safe_query(cfg, table_sql)

            col_sql = """
                SELECT
                    table_name,
                    column_name,
                    data_type,
                    is_nullable
                FROM information_schema.columns
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_name, ordinal_position;
            """
            col_rows = run_safe_query(cfg, col_sql)

            idx_sql = """
                SELECT
                    tablename AS table_name,
                    indexname AS index_name,
                    indexdef AS index_def
                FROM pg_indexes
                WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY tablename, indexname;
            """
            idx_rows = run_safe_query(cfg, idx_sql)

        elif db_type == "mysql":
            # MySQL schema inspection
            ver_rows = run_safe_query(cfg, "SELECT VERSION() AS version;")
            db_version = ver_rows[0].get("version", "") if ver_rows else ""

            table_sql = """
                SELECT
                    TABLE_NAME AS table_name,
                    COALESCE(TABLE_ROWS, 0) AS approx_row_count
                FROM information_schema.tables
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME;
            """
            table_rows = run_safe_query(cfg, table_sql, params=(cfg.database,))

            col_sql = """
                SELECT
                    TABLE_NAME AS table_name,
                    COLUMN_NAME AS column_name,
                    COLUMN_TYPE AS data_type,
                    IS_NULLABLE AS is_nullable
                FROM information_schema.columns
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, ORDINAL_POSITION;
            """
            col_rows = run_safe_query(cfg, col_sql, params=(cfg.database,))

            idx_sql = """
                SELECT
                    TABLE_NAME AS table_name,
                    INDEX_NAME AS index_name,
                    COLUMN_NAME AS index_def
                FROM information_schema.statistics
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;
            """
            idx_rows = run_safe_query(cfg, idx_sql, params=(cfg.database,))
        else:
            return f"ERROR: Unsupported db_type '{cfg.db_type}'"

        # Organize by table
        table_cols: dict[str, list[str]] = {}
        for c in col_rows:
            tname = str(c.get("table_name", ""))
            cname = str(c.get("column_name", ""))
            dtype = str(c.get("data_type", ""))
            nullable = str(c.get("is_nullable", "YES")).upper()
            null_str = " NULL" if nullable == "YES" else " NOT NULL"
            table_cols.setdefault(tname, []).append(f"{cname} {dtype}{null_str}")

        table_idxs: dict[str, list[str]] = {}
        for idx in idx_rows:
            tname = str(idx.get("table_name", ""))
            iname = str(idx.get("index_name", ""))
            idef = str(idx.get("index_def", ""))
            table_idxs.setdefault(tname, []).append(f"{iname}: {idef}")

        tables: list[TableInfo] = []
        for r in table_rows:
            tname = str(r.get("table_name", ""))
            row_cnt = max(0, int(r.get("approx_row_count", 0)))
            tables.append(
                TableInfo(
                    name=tname,
                    columns=table_cols.get(tname, []),
                    indexes=table_idxs.get(tname, []),
                    approximate_row_count=row_cnt,
                )
            )

        tool_context.state["schema_info"] = [t.model_dump() for t in tables]
        tool_context.state["db_version"] = db_version

        lines = [
            f"Database Engine: {cfg.db_type} ({db_version})",
            f"Database Name: {cfg.database}",
            f"Total Tables: {len(tables)}",
            "",
            "## Tables",
        ]
        for t in tables:
            lines.append(
                f"- **{t.name}**: ~{t.approximate_row_count} rows, {len(t.columns)} cols, {len(t.indexes)} indexes"
            )
            if t.columns:
                lines.append(f"  Columns: {', '.join(t.columns[:10])}{' ...' if len(t.columns) > 10 else ''}")
            if t.indexes:
                lines.append(f"  Indexes: {', '.join(t.indexes[:5])}{' ...' if len(t.indexes) > 5 else ''}")

        return "\n".join(lines)
    except Exception as e:
        return f"ERROR: failed to check schema: {e}"


def _categorize_mysql_variable(name: str) -> str:
    """Categorize MySQL global variable based on prefix."""
    lower = name.lower()
    if lower.startswith("innodb_buffer") or "pool" in lower:
        return "InnoDB / Buffer Pool & Memory"
    if lower.startswith("innodb_log") or lower.startswith("innodb_flush"):
        return "InnoDB / Redo Log & Flushing"
    if lower.startswith("innodb_io") or lower.startswith("innodb_read") or lower.startswith("innodb_write"):
        return "InnoDB / I/O & Threads"
    if lower.startswith("innodb_"):
        return "InnoDB Storage Engine"
    if lower.startswith("max_connections") or lower.startswith("table_open_cache") or lower.startswith("thread_"):
        return "Connections & Threading"
    if lower.startswith("join_buffer") or lower.startswith("sort_buffer") or lower.startswith("tmp_table"):
        return "Query Execution & Buffers"
    if lower.startswith("binlog_") or lower.startswith("sync_binlog") or lower.startswith("log_bin"):
        return "Replication & Binary Log"
    if lower.startswith("optimizer_") or "query" in lower:
        return "Query Optimizer"
    return "Global Settings"


def extract_knobs(tool_context: ToolContext) -> str:
    """Query active database settings and configuration knobs, saving to state.

    For Postgres: queries ``pg_settings``.
    For MySQL: queries ``performance_schema.global_variables`` or ``SHOW GLOBAL VARIABLES``.
    Writes extracted knobs to ``tool_context.state['knobs_info']``.
    """
    cfg = _get_db_config(tool_context)
    if not cfg:
        return "ERROR: DBConfig not found in state"

    db_type = cfg.db_type.lower()
    knobs: list[KnobInfo] = []

    try:
        if "post" in db_type:
            sql = """
                SELECT
                    name,
                    setting AS current_value,
                    COALESCE(unit, '') AS unit,
                    COALESCE(category, '') AS category,
                    COALESCE(short_desc, '') AS description,
                    COALESCE(min_val, '') AS min_val,
                    COALESCE(max_val, '') AS max_val,
                    COALESCE(context, '') AS context,
                    COALESCE(vartype, '') AS vartype,
                    COALESCE(enumvals, '{}') AS enumvals,
                    COALESCE(pending_restart, false) AS pending_restart,
                    COALESCE(boot_val, '') AS boot_val,
                    COALESCE(reset_val, '') AS reset_val
                FROM pg_settings
                ORDER BY category, name;
            """
            fallback_sql = """
                SELECT
                    name,
                    setting AS current_value,
                    COALESCE(unit, '') AS unit,
                    COALESCE(category, '') AS category,
                    COALESCE(short_desc, '') AS description,
                    COALESCE(min_val, '') AS min_val,
                    COALESCE(max_val, '') AS max_val,
                    COALESCE(context, '') AS context
                FROM pg_settings
                ORDER BY category, name;
            """
            try:
                rows = run_safe_query(cfg, sql)
            except Exception:
                rows = run_safe_query(cfg, fallback_sql)
            for r in rows:
                raw_enum = r.get("enumvals", [])
                if isinstance(raw_enum, str):
                    enumvals = [v.strip() for v in raw_enum.strip("{}").split(",") if v.strip()]
                elif isinstance(raw_enum, (list, tuple)):
                    enumvals = [str(v) for v in raw_enum]
                else:
                    enumvals = []
                raw_pending = r.get("pending_restart", False)
                if isinstance(raw_pending, str):
                    pending_restart = raw_pending.strip().lower() in ("t", "true")
                else:
                    pending_restart = bool(raw_pending)
                knobs.append(
                    KnobInfo(
                        name=str(r.get("name", "")),
                        current_value=str(r.get("current_value", "")),
                        unit=str(r.get("unit", "")),
                        category=str(r.get("category", "")),
                        description=str(r.get("description", "")),
                        min_val=str(r.get("min_val", "")),
                        max_val=str(r.get("max_val", "")),
                        context=str(r.get("context", "")),
                        vartype=str(r.get("vartype", "")),
                        enumvals=enumvals,
                        pending_restart=pending_restart,
                        boot_val=str(r.get("boot_val", "")),
                        reset_val=str(r.get("reset_val", "")),
                    )
                )

        elif db_type == "mysql":
            try:
                sql = "SELECT VARIABLE_NAME AS name, VARIABLE_VALUE AS current_value FROM performance_schema.global_variables ORDER BY VARIABLE_NAME;"
                rows = run_safe_query(cfg, sql)
            except Exception:
                rows = run_safe_query(cfg, "SHOW GLOBAL VARIABLES;")

            for r in rows:
                vname = str(r.get("name") or r.get("Variable_name") or "")
                vval = str(r.get("current_value") or r.get("Value") or "")
                category = _categorize_mysql_variable(vname)
                knobs.append(
                    KnobInfo(
                        name=vname,
                        current_value=vval,
                        unit="",
                        category=category,
                        description="",
                        min_val="",
                        max_val="",
                        context="global",
                    )
                )
        else:
            return f"ERROR: Unsupported db_type '{cfg.db_type}'"

        tool_context.state["knobs_info"] = [k.model_dump() for k in knobs]
        tool_context.state["available_knob_names"] = sorted(
            k.name for k in knobs if k.context.strip().lower() != "internal"
        )

        lines = [
            f"Extracted {len(knobs)} knobs from {cfg.db_type.upper()}.",
            "",
            "## Sample Tunable Knobs",
        ]
        sample_knobs = [
            k
            for k in knobs
            if any(
                term in k.name.lower()
                for term in (
                    "shared_buffers",
                    "work_mem",
                    "maintenance_work_mem",
                    "effective_cache_size",
                    "max_connections",
                    "max_wal_size",
                    "checkpoint_completion_target",
                    "random_page_cost",
                    "autovacuum",
                    "innodb_buffer_pool_size",
                    "innodb_log_file_size",
                    "innodb_flush_log_at_trx_commit",
                    "max_connections",
                    "join_buffer_size",
                    "sort_buffer_size",
                    "tmp_table_size",
                )
            )
        ]
        display_knobs = sample_knobs if sample_knobs else knobs[:20]
        for k in display_knobs:
            unit_str = f" {k.unit}" if k.unit else ""
            lines.append(f"- **{k.name}**: `{k.current_value}{unit_str}` ({k.category})")

        return "\n".join(lines)
    except Exception as e:
        return f"ERROR: failed to extract knobs: {e}"


def write_knobs_file(tool_context: ToolContext) -> str:
    """Write extracted knobs info from session state to ``{knob_path}/knobs.json``.

    Destination path is resolved from ``tool_context.state['knob_path']`` or
    ``tool_context.state['target']`` or current working directory.
    """
    knobs_info = tool_context.state.get("knobs_info")
    if not knobs_info:
        return "ERROR: knobs_info not found in state or empty"

    knob_path = tool_context.state.get("knob_path") or tool_context.state.get("target") or "."
    out_file = os.path.join(knob_path, "knobs.json")

    try:
        write_json_file(out_file, knobs_info)
        return f"OK: wrote {len(knobs_info)} knobs to {out_file}"
    except Exception as e:
        return f"ERROR: failed to write knobs file: {e}"
