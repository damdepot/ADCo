"""Pytest fixtures for knob_tuner unit tests."""

import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock

src_path = str(Path(__file__).resolve().parent.parent.parent / "src")
if src_path not in sys.path:
    sys.path.insert(0, src_path)

try:
    from knob_tuner.tools.db_connector import DBConfig
except ImportError:
    from src.knob_tuner.tools.db_connector import DBConfig


@pytest.fixture
def mock_db_config_pg() -> DBConfig:
    return DBConfig(
        host="127.0.0.1",
        port=5432,
        user="postgres",
        password="secretpassword",
        database="testdb",
        db_type="postgres",
        env="staging",
        restart_type="docker",
        restart_target="staging_postgres_container",
    )


@pytest.fixture
def mock_db_config_mysql() -> DBConfig:
    return DBConfig(
        host="127.0.0.1",
        port=3306,
        user="root",
        password="secretpassword",
        database="testdb",
        db_type="mysql",
        env="production",
        restart_type="systemctl",
        restart_target="mysql.service",
    )


@pytest.fixture
def sample_ini_path(tmp_path: Path) -> Path:
    ini_content = """
[production.mysql]
host = 127.0.0.1
port = 3306
user = prod_root
password = prod_pass
database = prod_db
restart_type = systemctl
restart_target = mysql

[staging.postgres]
host = 10.0.0.2
port = 5432
user = stg_postgres
password = stg_pass
database = stg_db
restart_type = docker
restart_target = stg_pg_container

[remote.postgres]
host = 192.168.1.100
port = 5432
user = remote_pg
password = remote_pass
database = remote_db
restart_type = ssh
remote_host = 192.168.1.100
remote_user = ubuntu
restart_cmd = sudo systemctl restart postgresql
"""
    config_file = tmp_path / "test_db.config"
    config_file.write_text(ini_content.strip(), encoding="utf-8")
    return config_file


@pytest.fixture
def mock_db_conn():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.description = [("id",), ("name",)]
    cursor.fetchall.return_value = [(1, "alpha"), (2, "beta")]
    cursor.fetchone.return_value = (1,)
    return conn, cursor
