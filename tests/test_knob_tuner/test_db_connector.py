"""Unit tests for db_connector module."""

import sys
from unittest.mock import MagicMock, patch
import pytest
from src.knob_tuner.tools.db_connector import (
    DBConfig,
    _is_safe_query,
    get_connection,
    load_db_config,
    run_safe_query,
)


def test_db_config_dataclass_defaults():
    cfg = DBConfig(
        host="localhost",
        port=5432,
        user="user",
        password="pwd",
        database="db",
        db_type="postgres",
        env="dev",
    )
    assert cfg.host == "localhost"
    assert cfg.port == 5432
    assert cfg.restart_type == "docker"
    assert cfg.restart_target == ""
    assert cfg.restart_cmd == ""
    assert cfg.remote_host == ""
    assert cfg.remote_user == ""


def test_load_db_config_postgres(sample_ini_path):
    cfg = load_db_config(str(sample_ini_path), env="staging", db_type="postgres")
    assert cfg.host == "10.0.0.2"
    assert cfg.port == 5432
    assert cfg.user == "stg_postgres"
    assert cfg.password == "stg_pass"
    assert cfg.database == "stg_db"
    assert cfg.db_type == "postgres"
    assert cfg.env == "staging"
    assert cfg.restart_type == "docker"
    assert cfg.restart_target == "stg_pg_container"


def test_load_db_config_mysql(sample_ini_path):
    cfg = load_db_config(str(sample_ini_path), env="production", db_type="mysql")
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 3306
    assert cfg.user == "prod_root"
    assert cfg.password == "prod_pass"
    assert cfg.database == "prod_db"
    assert cfg.db_type == "mysql"
    assert cfg.env == "production"
    assert cfg.restart_type == "systemctl"
    assert cfg.restart_target == "mysql"


def test_load_db_config_with_override(sample_ini_path):
    cfg = load_db_config(
        str(sample_ini_path),
        env="staging",
        db_type="postgres",
        db_override="custom_override_db",
    )
    assert cfg.database == "custom_override_db"


def test_load_db_config_remote_ssh(sample_ini_path):
    cfg = load_db_config(str(sample_ini_path), env="remote", db_type="postgres")
    assert cfg.restart_type == "ssh"
    assert cfg.remote_host == "192.168.1.100"
    assert cfg.remote_user == "ubuntu"
    assert cfg.restart_cmd == "sudo systemctl restart postgresql"


def test_load_db_config_file_not_found():
    with pytest.raises(FileNotFoundError, match="Database configuration file not found"):
        load_db_config("/path/to/non_existent_file.config", env="staging", db_type="postgres")


def test_load_db_config_missing_section(sample_ini_path):
    with pytest.raises(KeyError, match=r"Section \[non_existent\.postgres\] not found"):
        load_db_config(str(sample_ini_path), env="non_existent", db_type="postgres")


def test_load_db_config_missing_required_key(tmp_path):
    ini_content = """
[staging.postgres]
host = 127.0.0.1
port = 5432
user = postgres
# missing password and database
"""
    file_path = tmp_path / "bad.config"
    file_path.write_text(ini_content, encoding="utf-8")

    with pytest.raises(KeyError, match="Missing required key 'password'"):
        load_db_config(str(file_path), env="staging", db_type="postgres")


def test_load_db_config_invalid_port(tmp_path):
    ini_content = """
[staging.postgres]
host = 127.0.0.1
port = not_a_number
user = postgres
password = pass
database = db
"""
    file_path = tmp_path / "bad_port.config"
    file_path.write_text(ini_content, encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid port value"):
        load_db_config(str(file_path), env="staging", db_type="postgres")


def test_get_connection_postgres_psycopg2(mock_db_config_pg):
    mock_psycopg2 = MagicMock()
    mock_conn = MagicMock()
    mock_psycopg2.connect.return_value = mock_conn

    with patch.dict(sys.modules, {"psycopg2": mock_psycopg2}):
        conn = get_connection(mock_db_config_pg)
        assert conn == mock_conn
        mock_psycopg2.connect.assert_called_once_with(
            host=mock_db_config_pg.host,
            port=mock_db_config_pg.port,
            user=mock_db_config_pg.user,
            password=mock_db_config_pg.password,
            dbname=mock_db_config_pg.database,
        )


def test_get_connection_postgres_psycopg(mock_db_config_pg):
    mock_psycopg = MagicMock()
    mock_conn = MagicMock()
    mock_psycopg.connect.return_value = mock_conn

    with patch.dict(sys.modules, {"psycopg2": None, "psycopg": mock_psycopg}):
        conn = get_connection(mock_db_config_pg)
        assert conn == mock_conn
        mock_psycopg.connect.assert_called_once_with(
            host=mock_db_config_pg.host,
            port=mock_db_config_pg.port,
            user=mock_db_config_pg.user,
            password=mock_db_config_pg.password,
            dbname=mock_db_config_pg.database,
        )


def test_get_connection_postgres_missing_driver(mock_db_config_pg):
    with patch.dict(sys.modules, {"psycopg2": None, "psycopg": None}):
        with pytest.raises(ImportError, match="PostgreSQL driver not found"):
            get_connection(mock_db_config_pg)


def test_get_connection_mysql_pymysql(mock_db_config_mysql):
    mock_conn = MagicMock()
    with patch("pymysql.connect", return_value=mock_conn) as mock_connect:
        conn = get_connection(mock_db_config_mysql)
        assert conn == mock_conn
        mock_connect.assert_called_once()


def test_get_connection_mysql_mysqldb(mock_db_config_mysql):
    mock_mysqldb = MagicMock()
    mock_conn = MagicMock()
    mock_mysqldb.connect.return_value = mock_conn

    with patch.dict(sys.modules, {"pymysql": None, "MySQLdb": mock_mysqldb}):
        conn = get_connection(mock_db_config_mysql)
        assert conn == mock_conn
        mock_mysqldb.connect.assert_called_once()


def test_get_connection_mysql_missing_driver(mock_db_config_mysql):
    with patch.dict(sys.modules, {"pymysql": None, "MySQLdb": None}):
        with pytest.raises(ImportError, match="MySQL driver not found"):
            get_connection(mock_db_config_mysql)


def test_get_connection_unsupported_type():
    cfg = DBConfig(
        host="localhost",
        port=1234,
        user="u",
        password="p",
        database="d",
        db_type="oracle",
        env="dev",
    )
    with pytest.raises(ValueError, match="Unsupported db_type 'oracle'"):
        get_connection(cfg)


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT * FROM users;", True),
        ("  select count(*) from t ", True),
        ("SHOW TABLES", True),
        ("EXPLAIN SELECT 1", True),
        ("DESCRIBE users", True),
        ("DESC users", True),
        ("-- comment\nSELECT 1", True),
        ("/* multi-line \n comment */ SELECT 1", True),
        ("SELECT 1; SELECT 2;", True),
        ("INSERT INTO users VALUES (1);", False),
        ("UPDATE users SET x = 1;", False),
        ("DELETE FROM users WHERE id = 1;", False),
        ("DROP TABLE users;", False),
        ("ALTER TABLE users ADD col INT;", False),
        ("TRUNCATE TABLE users;", False),
        ("SELECT 1; DROP TABLE users;", False),
        ("", False),
    ],
)
def test_is_safe_query(sql, expected):
    assert _is_safe_query(sql) == expected


def test_run_safe_query_success_tuple_cursor(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    cursor.description = [("id",), ("name",)]
    cursor.fetchall.return_value = [(10, "alice"), (20, "bob")]

    with patch("src.knob_tuner.tools.db_connector.get_connection", return_value=conn):
        rows = run_safe_query(mock_db_config_pg, "SELECT id, name FROM users;")
        assert rows == [{"id": 10, "name": "alice"}, {"id": 20, "name": "bob"}]
        cursor.execute.assert_called_once_with("SELECT id, name FROM users;")
        cursor.close.assert_called_once()
        conn.close.assert_called_once()


def test_run_safe_query_success_dict_cursor(mock_db_config_mysql, mock_db_conn):
    conn, cursor = mock_db_conn
    cursor.description = [("id",), ("name",)]
    cursor.fetchall.return_value = [{"id": 1, "name": "alice"}]

    with patch("src.knob_tuner.tools.db_connector.get_connection", return_value=conn):
        rows = run_safe_query(mock_db_config_mysql, "SHOW VARIABLES LIKE '%max%';")
        assert rows == [{"id": 1, "name": "alice"}]


def test_run_safe_query_with_params(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    cursor.description = [("val",)]
    cursor.fetchall.return_value = [(42,)]

    with patch("src.knob_tuner.tools.db_connector.get_connection", return_value=conn):
        rows = run_safe_query(
            mock_db_config_pg, "SELECT val FROM t WHERE id = %s", params=(1,)
        )
        assert rows == [{"val": 42}]
        cursor.execute.assert_called_once_with("SELECT val FROM t WHERE id = %s", (1,))


def test_run_safe_query_empty_results(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    cursor.description = [("val",)]
    cursor.fetchall.return_value = []

    with patch("src.knob_tuner.tools.db_connector.get_connection", return_value=conn):
        rows = run_safe_query(mock_db_config_pg, "SELECT val FROM t WHERE 1=0;")
        assert rows == []


def test_run_safe_query_no_description(mock_db_config_pg, mock_db_conn):
    conn, cursor = mock_db_conn
    cursor.description = None

    with patch("src.knob_tuner.tools.db_connector.get_connection", return_value=conn):
        rows = run_safe_query(mock_db_config_pg, "SELECT 1")
        assert rows == []


def test_run_safe_query_rejects_unsafe(mock_db_config_pg):
    with pytest.raises(ValueError, match="Unsafe query rejected"):
        run_safe_query(mock_db_config_pg, "DROP TABLE users;")
