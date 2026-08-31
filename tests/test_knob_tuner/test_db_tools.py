"""Unit tests for db_tools module."""

from unittest.mock import MagicMock, patch
import pytest
from src.knob_tuner.tools.db_connector import DBConfig
from src.knob_tuner.tools.db_tools import apply_knobs, test_database as run_test_database


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
        assert cursor.execute.call_count == 2
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
