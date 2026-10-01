"""Unit tests for db_tools module."""

from unittest.mock import patch
from src.knob_tuner.contracts import ApplyMode, KnobScope
from src.knob_tuner.tools.db_connector import DBConfig
from src.knob_tuner.tools.db_tools import (
    apply_knobs,
    is_noop_value,
    snapshot_settings,
    verify_active_knobs,
    _parse_time_to_ms,
    _parse_enumvals,
)


def test_apply_knobs_dry_run_postgres(mock_db_config_pg):
    knobs = [
        {"name": "shared_buffers", "value": "256MB"},
        {"name": "max_connections", "value": 100},
        {"name": "enable_seqscan", "value": "off"},
    ]
    results = apply_knobs(knobs, mock_db_config_pg, dry_run=True)
    assert len(results) == 3
    assert results[0] == {
        "knob": "shared_buffers",
        "value": "256MB",
        "status": "dry_run",
        "sql": "ALTER SYSTEM SET shared_buffers = '256MB';",
        "error": None,
    }
    assert results[1] == {
        "knob": "max_connections",
        "value": 100,
        "status": "dry_run",
        "sql": "ALTER SYSTEM SET max_connections = 100;",
        "error": None,
    }
    assert results[2] == {
        "knob": "enable_seqscan",
        "value": "off",
        "status": "dry_run",
        "sql": "ALTER SYSTEM SET enable_seqscan = off;",
        "error": None,
    }


def test_apply_knobs_dry_run_mysql(mock_db_config_mysql):
    knobs = [
        {"name": "innodb_buffer_pool_size", "value": "1073741824"},
        {"name": "max_connections", "value": 200},
        {"name": "autocommit", "value": 1},
    ]
    results = apply_knobs(knobs, mock_db_config_mysql, dry_run=True)
    assert len(results) == 3
    assert results[0]["sql"] == "SET GLOBAL innodb_buffer_pool_size = 1073741824;"
    assert results[1]["sql"] == "SET GLOBAL max_connections = 200;"
    assert results[2]["sql"] == "SET GLOBAL autocommit = 1;"


def test_apply_knobs_escapes_malicious_value_postgres(mock_db_config_pg):
    knobs = [{"name": "work_mem", "value": "1'; DROP TABLE users; --"}]
    results = apply_knobs(knobs, mock_db_config_pg, dry_run=True)
    assert results[0]["sql"] == (
        "ALTER SYSTEM SET work_mem = '1''; DROP TABLE users; --';"
    )


def test_apply_knobs_escapes_malicious_value_mysql(mock_db_config_mysql):
    knobs = [{"name": "work_mem", "value": "1'; DROP TABLE users; --"}]
    results = apply_knobs(knobs, mock_db_config_mysql, dry_run=True)
    assert results[0]["sql"] == (
        "SET GLOBAL work_mem = '1''; DROP TABLE users; --';"
    )


def test_apply_knobs_empty_list(mock_db_config_pg):
    assert apply_knobs([], mock_db_config_pg) == []


def test_apply_knobs_live_execution_postgres(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    knobs = [
        {"name": "work_mem", "value": "64MB"},
        {"knob": "maintenance_work_mem", "value": "128MB"},
    ]
    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        results = apply_knobs(knobs, mock_db_config_pg, dry_run=False)
        assert len(results) == 2
        assert results[0]["status"] == "applied"
        assert results[0]["sql"] == "ALTER SYSTEM SET work_mem = '64MB';"
        assert results[1]["status"] == "applied"
        assert results[1]["sql"] == "ALTER SYSTEM SET maintenance_work_mem = '128MB';"
        assert cursor.execute.call_count == 3
        cursor.close.assert_called_once()
        conn.close.assert_called_once()


def test_apply_knobs_live_execution_mysql(mock_db_config_mysql, mock_db_conn):
    conn, cursor = mock_db_conn
    knobs = [{"name": "max_connections", "value": 500}]
    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        results = apply_knobs(knobs, mock_db_config_mysql, dry_run=False)
        assert len(results) == 1
        assert results[0]["status"] == "applied"
        assert results[0]["sql"] == "SET GLOBAL max_connections = 500;"
        cursor.execute.assert_called_once_with("SET GLOBAL max_connections = 500;")


def test_apply_knobs_handles_partial_failures(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    cursor.execute.side_effect = [Exception("Syntax error near 'INVALID'"), None]

    knobs = [
        {"name": "invalid_knob", "value": "BAD"},
        {"name": "work_mem", "value": "32MB"},
    ]
    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        results = apply_knobs(knobs, mock_db_config_pg, dry_run=False)
        assert len(results) == 2
        assert results[0]["status"] == "failed"
        assert "Syntax error" in results[0]["error"]
        assert results[1]["status"] == "applied"
        assert results[1]["error"] is None


def test_apply_knobs_unsupported_db_type():
    cfg = DBConfig(
        host="localhost",
        port=1234,
        user="u",
        password="p",
        database="d",
        db_type="sqlite",
        env="dev",
    )
    results = apply_knobs([{"name": "cache_size", "value": 1000}], cfg, dry_run=True)
    assert len(results) == 1
    assert results[0]["status"] == "failed"
    assert "Unsupported db_type" in results[0]["error"]


def test_apply_knobs_mode_none_skips_all_no_connection(mock_db_config_pg):
    knobs = [
        {"name": "work_mem", "value": "64MB", "scope": "user"},
        {"name": "shared_buffers", "value": "1GB", "scope": "postmaster"},
        {"name": "max_worker_processes", "value": 8, "scope": "internal"},
    ]
    with patch("src.knob_tuner.tools.db_tools.get_connection") as mock_conn:
        results = apply_knobs(knobs, mock_db_config_pg, mode=ApplyMode.NONE)
    mock_conn.assert_not_called()
    assert len(results) == 3
    for result in results:
        assert result["status"] == "skipped"
        assert result["sql"] == ""
        assert result["error"] is None


def test_apply_knobs_mode_live_excludes_static_and_rejects_internal(
    mock_db_config_pg, mock_db_conn
):
    conn, cursor = mock_db_conn
    knobs = [
        {"name": "work_mem", "value": "64MB", "scope": "user"},
        {"name": "shared_buffers", "value": "1GB", "scope": KnobScope.POSTMASTER},
        {"name": "max_worker_processes", "value": 8, "scope": "internal"},
    ]
    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        results = apply_knobs(knobs, mock_db_config_pg, mode=ApplyMode.LIVE)

    assert len(results) == 3
    assert results[0]["status"] == "applied"
    assert results[1]["status"] == "skipped"
    assert results[2]["status"] == "failed"
    assert results[2]["error"] == "internal knob rejected: max_worker_processes"
    assert results[2]["sql"] == ""
    # One ALTER for work_mem + one pg_reload_conf()
    assert cursor.execute.call_count == 2


def test_apply_knobs_mode_manual_applies_full_plan_and_reloads(
    mock_db_config_pg, mock_db_conn
):
    conn, cursor = mock_db_conn
    knobs = [
        {"name": "work_mem", "value": "64MB", "scope": "user"},
        {"name": "shared_buffers", "value": "1GB", "scope": "postmaster"},
        {"name": "max_worker_processes", "value": 8, "scope": "internal"},
    ]
    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        results = apply_knobs(knobs, mock_db_config_pg, mode=ApplyMode.MANUAL)

    # Reloadable knobs go live; restart-required knobs are persisted (pending a
    # manual restart); internal knobs are rejected.
    assert results[0]["status"] == "applied"
    assert results[1]["status"] == "applied"
    assert results[2]["status"] == "failed"
    # Two ALTERs + reload.
    assert cursor.execute.call_count == 3
    assert cursor.execute.call_args_list[-1].args[0] == "SELECT pg_reload_conf();"


def test_apply_knobs_internal_rejected_in_dry_run(mock_db_config_pg):
    knobs = [{"name": "max_worker_processes", "value": 8, "scope": "internal"}]
    results = apply_knobs(knobs, mock_db_config_pg, dry_run=True)
    assert results[0]["status"] == "failed"
    assert results[0]["error"] == "internal knob rejected: max_worker_processes"
    assert results[0]["sql"] == ""


def test_snapshot_settings_postgres(mock_db_config_pg):
    rows = [
        {"name": "work_mem", "setting": "4096"},
        {"name": "shared_buffers", "setting": "16384"},
    ]
    with patch(
        "src.knob_tuner.tools.db_tools.run_safe_query", return_value=rows
    ) as mock_query:
        snapshot = snapshot_settings(
            mock_db_config_pg, ["work_mem", "shared_buffers", "missing"]
        )
    assert snapshot == {"work_mem": "4096", "shared_buffers": "16384"}
    assert "pg_settings" in mock_query.call_args.args[1]


def test_snapshot_settings_mysql_case_insensitive(mock_db_config_mysql):
    rows = [
        {"VARIABLE_NAME": "MAX_CONNECTIONS", "VARIABLE_VALUE": "151"},
        {"VARIABLE_NAME": "innodb_buffer_pool_size", "VARIABLE_VALUE": "134217728"},
    ]
    with patch("src.knob_tuner.tools.db_tools.run_safe_query", return_value=rows):
        snapshot = snapshot_settings(
            mock_db_config_mysql, ["max_connections", "INNODB_BUFFER_POOL_SIZE"]
        )
    assert snapshot["max_connections"] == "151"
    assert snapshot["INNODB_BUFFER_POOL_SIZE"] == "134217728"


def test_snapshot_settings_returns_empty_on_error(mock_db_config_pg):
    with patch(
        "src.knob_tuner.tools.db_tools.run_safe_query",
        side_effect=Exception("connection refused"),
    ):
        assert snapshot_settings(mock_db_config_pg, ["work_mem"]) == {}


def test_snapshot_settings_empty_names(mock_db_config_pg):
    with patch("src.knob_tuner.tools.db_tools.run_safe_query") as mock_query:
        assert snapshot_settings(mock_db_config_pg, []) == {}
    mock_query.assert_not_called()


def test_parse_time_to_ms():
    assert _parse_time_to_ms("1000000", "us") == 1000.0
    assert _parse_time_to_ms("1s") == 1000.0
    assert _parse_time_to_ms("1000", "ms") == 1000.0

def test_parse_enumvals():
    assert _parse_enumvals(["a", "b"]) == {"a", "b"}
    assert _parse_enumvals(("a", "b")) == {"a", "b"}
    assert _parse_enumvals("{off,pglz,lz4,zstd,on}") == {"off", "pglz", "lz4", "zstd", "on"}
    assert _parse_enumvals("a, b, c") == {"a", "b", "c"}
    assert _parse_enumvals("") == set()
    assert _parse_enumvals(None) == set()

def test_verify_active_knobs_postgres_verified(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    
    # Mock pg_settings result
    # Columns: name, setting, unit, boot_val, reset_val, pending_restart, vartype, enumvals, context
    cursor.fetchall.return_value = [
        ("shared_buffers", "65536", "8kB", "1024", "65536", False, "integer", None, "postmaster"),
        ("work_mem", "4096", "kB", "4096", "4096", False, "integer", None, "user"),
        ("random_page_cost", "0.9", "", "4.0", "0.9", False, "real", None, "user"),
        ("enable_seqscan", "on", "", "on", "on", False, "bool", None, "user"),
    ]

    expected = [
        {"name": "shared_buffers", "value": "512MB"},
        {"name": "work_mem", "value": "4MB"},
        {"name": "random_page_cost", "value": "0.90"},
        {"name": "enable_seqscan", "value": "on"}
    ]

    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        res = verify_active_knobs(mock_db_config_pg, expected)
        assert res["status"] == "ok"
        assert res["all_verified"] is True
        for knob in res["knobs"]:
            assert knob["status"] == "VERIFIED"

def test_verify_active_knobs_postgres_mismatch(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    
    cursor.fetchall.return_value = [
        ("shared_buffers", "32768", "8kB", "1024", "32768", False, "integer", None, "postmaster")
    ]

    expected = [
        {"name": "shared_buffers", "value": "512MB"}
    ]

    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        res = verify_active_knobs(mock_db_config_pg, expected)
        assert res["status"] == "ok"
        assert res["all_verified"] is False
        assert res["knobs"][0]["status"] == "MISMATCH"

def test_verify_active_knobs_postgres_pending_restart(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    
    cursor.fetchall.return_value = [
        ("shared_buffers", "65536", "8kB", "1024", "65536", True, "integer", None, "postmaster")
    ]

    expected = [
        {"name": "shared_buffers", "value": "512MB"}
    ]

    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        res = verify_active_knobs(mock_db_config_pg, expected)
        assert res["status"] == "ok"
        assert res["all_verified"] is False
        assert res["knobs"][0]["status"] == "PENDING_RESTART"

def test_verify_active_knobs_postgres_new_comparisons(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    
    # Testing new comparison logic for enums, strings, integers with units
    cursor.fetchall.return_value = [
        ("wal_compression", "pglz", "", "off", "pglz", False, "enum", "{off,pglz,lz4,zstd,on}", "user"),
        ("synchronous_commit", "on", "", "on", "on", False, "enum", "{local,remote_write,remote_apply,on,off}", "user"),
        ("default_transaction_isolation", "read committed", "", "read committed", "read committed", False, "enum", "{serializable,repeatable read,read committed,read uncommitted}", "user"),
        ("shared_preload_libraries", "pg_stat_statements,auto_explain", "", "", "pg_stat_statements,auto_explain", False, "string", None, "postmaster"),
        ("lock_timeout", "1000000", "us", "0", "1000000", False, "integer", None, "user"),
        ("statement_timeout", "30000000", "us", "0", "30000000", False, "integer", None, "user"),
    ]
    
    expected = [
        {"name": "wal_compression", "value": "on"},
        {"name": "synchronous_commit", "value": "on"},
        {"name": "default_transaction_isolation", "value": "read committed"},
        {"name": "shared_preload_libraries", "value": "pg_stat_statements, auto_explain"},
        {"name": "lock_timeout", "value": "1s"},
        {"name": "statement_timeout", "value": "30s"}
    ]
    
    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        res = verify_active_knobs(mock_db_config_pg, expected)
        assert res["status"] == "ok"
        assert res["all_verified"] is True
        for knob in res["knobs"]:
            assert knob["status"] == "VERIFIED"
            
    # test mismatches
    cursor.fetchall.return_value = [
        ("wal_compression", "off", "", "off", "off", False, "enum", "{off,pglz,lz4,zstd,on}", "user"),
        ("lock_timeout", "999999", "us", "0", "999999", False, "integer", None, "user"),
    ]
    expected_mismatch = [
        {"name": "wal_compression", "value": "on"},
        {"name": "lock_timeout", "value": "1s"},
    ]
    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        res = verify_active_knobs(mock_db_config_pg, expected_mismatch)
        assert res["status"] == "ok"
        assert res["all_verified"] is False
        assert res["knobs"][0]["status"] == "MISMATCH"
        assert res["knobs"][1]["status"] == "MISMATCH"
        
    # check off vs off and lz4 vs lz4
    cursor.fetchall.return_value = [
        ("wal_compression", "off", "", "off", "off", False, "enum", "{off,pglz,lz4,zstd,on}", "user"),
    ]
    expected_off = [{"name": "wal_compression", "value": "off"}]
    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        res = verify_active_knobs(mock_db_config_pg, expected_off)
        assert res["status"] == "ok"
        assert res["knobs"][0]["status"] == "VERIFIED"
        
    cursor.fetchall.return_value = [
        ("wal_compression", "lz4", "", "off", "lz4", False, "enum", "{off,pglz,lz4,zstd,on}", "user"),
    ]
    expected_lz4 = [{"name": "wal_compression", "value": "lz4"}]
    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        res = verify_active_knobs(mock_db_config_pg, expected_lz4)
        assert res["status"] == "ok"
        assert res["knobs"][0]["status"] == "VERIFIED"

def test_verify_active_knobs_postgres_fallback_query(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    # Mock execute to raise exception on the first query
    def mock_execute(query, *args, **kwargs):
        if "vartype" in query:
            raise Exception("Column vartype does not exist")
    cursor.execute.side_effect = mock_execute
    
    cursor.fetchall.return_value = [
        ("shared_buffers", "65536", "8kB", "1024", "65536", False),
    ]
    
    expected = [{"name": "shared_buffers", "value": "512MB"}]
    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        res = verify_active_knobs(mock_db_config_pg, expected)
        assert res["status"] == "ok"
        assert res["all_verified"] is True

def test_verify_active_knobs_mysql(mock_db_config_mysql, mock_db_conn):
    conn, cursor = mock_db_conn
    
    # Columns: VARIABLE_NAME, VARIABLE_VALUE
    cursor.fetchall.return_value = [
        ("innodb_buffer_pool_size", "1073741824"),
        ("max_connections", "200")
    ]

    expected = [
        {"name": "innodb_buffer_pool_size", "value": "1073741824"},
        {"name": "max_connections", "value": "200"}
    ]

    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        res = verify_active_knobs(mock_db_config_mysql, expected)
        assert res["status"] == "ok"
        assert res["all_verified"] is True
        for knob in res["knobs"]:
            assert knob["status"] == "VERIFIED"


def test_is_noop_value_equivalent_memory_units():
    entry = {"current_value": "524288", "unit": "8kB", "vartype": "integer"}
    assert is_noop_value(entry, "4GB") is True
    assert is_noop_value(entry, "4096MB") is True
    assert is_noop_value(entry, "524288") is True


def test_is_noop_value_rejects_genuine_change():
    entry = {"current_value": "524288", "unit": "8kB", "vartype": "integer"}
    assert is_noop_value(entry, "8GB") is False


def test_is_noop_value_reads_setting_alias():
    entry = {"setting": "4", "vartype": "integer"}
    assert is_noop_value(entry, 4) is True
    assert is_noop_value(entry, 5) is False


def test_is_noop_value_missing_current_returns_false():
    assert is_noop_value({}, "4GB") is False
    assert is_noop_value({"current_value": ""}, "4GB") is False


def test_is_noop_value_unknown_unit_falls_back_conservatively():
    entry = {"current_value": "4", "unit": "furlongs", "vartype": ""}
    assert is_noop_value(entry, "4") is True
    assert is_noop_value(entry, "4xyz") is False


def test_is_noop_value_bool_and_enum_equivalence():
    assert is_noop_value({"current_value": "on", "vartype": "bool"}, "true") is True
    assert (
        is_noop_value(
            {"current_value": "off", "vartype": "bool"},
            "on",
        )
        is False
    )
    enum_entry = {
        "current_value": "lz4",
        "vartype": "enum",
        "enumvals": ["off", "pglz", "lz4"],
    }
    assert is_noop_value(enum_entry, "lz4") is True
    assert is_noop_value(enum_entry, "on") is False
