"""Unit tests for restart_tools module."""

import subprocess
from unittest.mock import MagicMock, patch
import pytest
from src.knob_tuner.tools.db_connector import DBConfig
from src.knob_tuner.tools.restart_tools import (
    restart_db_by_config,
    restart_docker_db,
    restart_local_db,
    restart_remote_db,
)


def test_restart_docker_db_success():
    mock_res = MagicMock(returncode=0, stdout="postgres_db\n", stderr="")
    with patch("subprocess.run", return_value=mock_res) as mock_run:
        ok, msg = restart_docker_db("postgres_db")
        assert ok is True
        assert "restarted successfully" in msg
        mock_run.assert_called_once_with(
            ["docker", "restart", "postgres_db"],
            capture_output=True,
            text=True,
            timeout=60,
        )


def test_restart_docker_db_failure():
    mock_res = MagicMock(
        returncode=1, stdout="", stderr="Error: No such container: bad_container"
    )
    with patch("subprocess.run", return_value=mock_res):
        ok, msg = restart_docker_db("bad_container")
        assert ok is False
        assert "Failed to restart container 'bad_container'" in msg
        assert "No such container" in msg


def test_restart_docker_db_empty_name():
    ok, msg = restart_docker_db("   ")
    assert ok is False
    assert "Container name cannot be empty" in msg


def test_restart_docker_db_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=5)):
        ok, msg = restart_docker_db("pg_cont", timeout=5)
        assert ok is False
        assert "Timed out" in msg


def test_restart_docker_db_not_found():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        ok, msg = restart_docker_db("pg_cont")
        assert ok is False
        assert "docker command not found" in msg


def test_restart_local_db_systemctl_success():
    mock_res = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=mock_res) as mock_run:
        ok, msg = restart_local_db("postgresql", method="systemctl")
        assert ok is True
        assert "restarted successfully via systemctl" in msg
        mock_run.assert_called_once_with(
            ["systemctl", "restart", "postgresql"],
            capture_output=True,
            text=True,
            timeout=60,
        )


def test_restart_local_db_service_method():
    mock_res = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=mock_res) as mock_run:
        ok, msg = restart_local_db("mysql", method="service")
        assert ok is True
        mock_run.assert_called_once_with(
            ["service", "mysql", "restart"],
            capture_output=True,
            text=True,
            timeout=60,
        )


def test_restart_local_db_brew_method():
    mock_res = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=mock_res) as mock_run:
        ok, msg = restart_local_db("postgresql@14", method="brew")
        assert ok is True
        mock_run.assert_called_once_with(
            ["brew", "services", "restart", "postgresql@14"],
            capture_output=True,
            text=True,
            timeout=60,
        )


def test_restart_local_db_empty_name():
    ok, msg = restart_local_db("")
    assert ok is False
    assert "Service name cannot be empty" in msg


def test_restart_local_db_failure():
    mock_res = MagicMock(returncode=1, stdout="", stderr="Job failed")
    with patch("subprocess.run", return_value=mock_res):
        ok, msg = restart_local_db("bad_service")
        assert ok is False
        assert "Failed to restart service" in msg


def test_restart_local_db_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=10)):
        ok, msg = restart_local_db("mysql", timeout=10)
        assert ok is False
        assert "Timed out" in msg


def test_restart_remote_db_success():
    mock_res = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=mock_res) as mock_run:
        ok, msg = restart_remote_db(
            host="192.168.1.50",
            user="admin",
            restart_cmd="systemctl restart postgresql",
            key_file="/path/to/key.pem",
        )
        assert ok is True
        assert "restarted successfully" in msg
        mock_run.assert_called_once_with(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=no",
                "-i",
                "/path/to/key.pem",
                "admin@192.168.1.50",
                "systemctl restart postgresql",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )


def test_restart_remote_db_missing_args():
    ok, msg = restart_remote_db("", "admin", "restart")
    assert ok is False
    assert "Remote host cannot be empty" in msg

    ok, msg = restart_remote_db("host", "", "restart")
    assert ok is False
    assert "Remote user cannot be empty" in msg

    ok, msg = restart_remote_db("host", "admin", "")
    assert ok is False
    assert "Restart command cannot be empty" in msg


def test_restart_remote_db_failure():
    mock_res = MagicMock(returncode=255, stdout="", stderr="Permission denied (publickey)")
    with patch("subprocess.run", return_value=mock_res):
        ok, msg = restart_remote_db("host.example.com", "ubuntu", "sudo systemctl restart pg")
        assert ok is False
        assert "Failed to restart remote database" in msg


def test_restart_db_by_config_docker(mock_db_config_pg):
    with patch("src.knob_tuner.tools.restart_tools.restart_docker_db", return_value=(True, "ok")) as mock_docker:
        ok, msg = restart_db_by_config(mock_db_config_pg)
        assert ok is True
        mock_docker.assert_called_once_with("staging_postgres_container")


def test_restart_db_by_config_local(mock_db_config_mysql):
    with patch("src.knob_tuner.tools.restart_tools.restart_local_db", return_value=(True, "ok")) as mock_local:
        ok, msg = restart_db_by_config(mock_db_config_mysql)
        assert ok is True
        mock_local.assert_called_once_with("mysql.service", method="systemctl")


def test_restart_db_by_config_remote():
    cfg = DBConfig(
        host="10.0.0.1",
        port=5432,
        user="pg",
        password="pwd",
        database="db",
        db_type="postgres",
        env="prod",
        restart_type="ssh",
        remote_host="10.0.0.1",
        remote_user="admin",
        restart_cmd="sudo service postgresql restart",
    )
    with patch("src.knob_tuner.tools.restart_tools.restart_remote_db", return_value=(True, "ok")) as mock_remote:
        ok, msg = restart_db_by_config(cfg)
        assert ok is True
        mock_remote.assert_called_once_with(
            host="10.0.0.1",
            user="admin",
            restart_cmd="sudo service postgresql restart",
        )


def test_restart_db_by_config_unsupported():
    cfg = DBConfig(
        host="10.0.0.1",
        port=5432,
        user="pg",
        password="pwd",
        database="db",
        db_type="postgres",
        env="prod",
        restart_type="kubernetes_pod_delete",
    )
    ok, msg = restart_db_by_config(cfg)
    assert ok is False
    assert "Unsupported restart_type" in msg
