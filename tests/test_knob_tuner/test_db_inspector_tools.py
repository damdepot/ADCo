"""Unit test for db_inspector schema inspection ordering."""

from unittest.mock import patch

from src.knob_tuner.sub_agents.db_inspector.tools import (
    _get_db_config,
    check_schema,
    extract_knobs,
)


class _Ctx:
    def __init__(self, state):
        self.state = state


def test_check_schema_analyzes_before_reading_row_estimates():
    order: list[tuple[str, str]] = []

    def fake_query(cfg, sql, params=None):
        order.append(("query", sql))
        low = sql.lower()
        if "version()" in low:
            return [{"version": "PostgreSQL 17"}]
        if "from pg_class" in low:
            return [{"table_name": "orders", "approx_row_count": 111}]
        return []

    def fake_analyze(cfg):
        order.append(("analyze", ""))
        return True

    ctx = _Ctx(
        {
            "db_config": {
                "db_type": "postgres",
                "host": "h",
                "port": 5432,
                "user": "u",
                "password": "p",
                "database": "d",
            }
        }
    )
    with patch(
        "src.knob_tuner.sub_agents.db_inspector.tools.run_safe_query",
        side_effect=fake_query,
    ), patch(
        "src.knob_tuner.sub_agents.db_inspector.tools.analyze_database",
        side_effect=fake_analyze,
    ):
        output = check_schema(ctx)

    kinds = [kind for kind, _ in order]
    analyze_idx = kinds.index("analyze")
    table_idx = next(
        i
        for i, (kind, sql) in enumerate(order)
        if kind == "query" and "from pg_class" in sql.lower()
    )
    assert analyze_idx < table_idx
    assert "orders" in output


def _pg_context():
    return _Ctx(
        {
            "db_config": {
                "db_type": "postgres",
                "host": "h",
                "port": 5432,
                "user": "u",
                "password": "p",
                "database": "d",
            }
        }
    )


def _pg_row(**overrides):
    row = {
        "name": "synchronous_commit",
        "current_value": "on",
        "unit": "",
        "category": "Write-Ahead Log",
        "description": "Sets the synchronization level of commits.",
        "min_val": "",
        "max_val": "",
        "context": "user",
        "vartype": "enum",
        "enumvals": "{on,off,local}",
        "pending_restart": False,
        "boot_val": "on",
        "reset_val": "on",
    }
    row.update(overrides)
    return row


def test_extract_knobs_parses_extended_fields_and_exposes_names():
    ctx = _pg_context()
    with patch(
        "src.knob_tuner.sub_agents.db_inspector.tools.run_safe_query",
        return_value=[_pg_row()],
    ):
        extract_knobs(ctx)

    info = ctx.state["knobs_info"][0]
    assert info["vartype"] == "enum"
    assert info["enumvals"] == ["on", "off", "local"]
    assert info["pending_restart"] is False
    assert "synchronous_commit" in ctx.state["available_knob_names"]


def test_extract_knobs_excludes_internal_from_available_names():
    ctx = _pg_context()
    with patch(
        "src.knob_tuner.sub_agents.db_inspector.tools.run_safe_query",
        return_value=[_pg_row(name="block_size", context="internal")],
    ):
        extract_knobs(ctx)

    assert ctx.state["knobs_info"][0]["name"] == "block_size"
    assert "block_size" not in ctx.state["available_knob_names"]


def test_get_db_config_rebuilds_from_path_when_state_is_redacted(tmp_path):
    config = tmp_path / "db.config"
    config.write_text(
        "[postgres]\nhost=h\nport=5432\nuser=u\npassword=secret\n",
        encoding="utf-8",
    )
    ctx = _Ctx(
        {
            "db_config": {
                "db_type": "postgres",
                "host": "h",
                "port": 5432,
                "user": "u",
                "password": "",
                "database": "mydb",
            },
            "db_config_path": str(config),
            "db_type": "postgres",
            "database": "mydb",
        }
    )

    cfg = _get_db_config(ctx)

    assert cfg is not None
    assert cfg.password == "secret"
    assert cfg.database == "mydb"


def test_get_db_config_prefers_fully_populated_state_config():
    ctx = _Ctx(
        {
            "db_config": {
                "db_type": "postgres",
                "host": "h",
                "port": 5432,
                "user": "u",
                "password": "instate",
                "database": "d",
            }
        }
    )

    cfg = _get_db_config(ctx)

    assert cfg is not None
    assert cfg.password == "instate"
