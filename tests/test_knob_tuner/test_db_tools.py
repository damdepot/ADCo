"""Unit tests for db_tools module."""

from unittest.mock import MagicMock, patch
import pytest
from src.knob_tuner.tools.db_connector import DBConfig
from src.knob_tuner.tools.db_tools import apply_knobs, test_database as run_test_database, verify_active_knobs, _parse_time_to_ms, _parse_enumvals


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


def test_test_database_success(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    # Returns for:
    # 1. SELECT 1 -> (1,)
    # 2. SELECT table_name -> [('users',), ('posts',)]
    # 3. SELECT val -> ('health_test',)
    cursor.fetchone.side_effect = [(1,), ("health_test",)]
    cursor.fetchall.return_value = [("users",), ("posts",)]

    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        res = run_test_database(mock_db_config_pg)
        assert res["status"] == "ok"
        assert res["checks"]["connectivity"] is True
        assert res["checks"]["ping"] is True
        assert res["checks"]["table_scan"] is True
        assert res["checks"]["crud"] is True
        assert res["details"]["tables_found"] == ["users", "posts"]
        assert res["details"]["crud_result"] == "passed"
        assert res["error"] is None


def test_test_database_connectivity_failure(mock_db_config_pg):
    with patch("src.knob_tuner.tools.db_tools.get_connection", side_effect=Exception("Connection refused")):
        res = run_test_database(mock_db_config_pg)
        assert res["status"] == "error"
        assert res["checks"]["connectivity"] is False
        assert res["error"] == "Connection refused"


def test_test_database_crud_failure_and_cleanup(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    # Ping succeeds, schema scan succeeds, CRUD select returns unexpected value
    cursor.fetchone.side_effect = [(1,), ("wrong_value",)]
    cursor.fetchall.return_value = [{"table_name": "t1"}]

    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        res = run_test_database(mock_db_config_pg)
        assert res["status"] == "error"
        assert res["checks"]["connectivity"] is True
        assert res["checks"]["ping"] is True
        assert res["checks"]["table_scan"] is True
        assert res["checks"]["crud"] is False
        assert "CRUD test failed" in res["error"]


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
