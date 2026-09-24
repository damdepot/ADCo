"""Unit tests for docker_tools module."""

import os
import subprocess
from unittest.mock import MagicMock, call, patch
import pytest

from src.knob_tuner.contracts import ResourceBudget
from src.knob_tuner.tools.db_connector import DBConfig
from src.knob_tuner.tools.docker_tools import (
    cleanup_orphan_containers,
    is_docker_available,
    resolve_docker_image,
    start_staging_db,
    stop_staging_db,
    get_container_host_port,
    verify_container_resources,
)


def budget(cpu_cores: int = 2, memory_gb: float = 2) -> ResourceBudget:
    """Convenience constructor for test resource budgets."""
    return ResourceBudget(cpu_cores=cpu_cores, memory_gb=memory_gb)


VERIFY_OK = (True, "resources verified")


# =====================================================================
# is_docker_available tests
# =====================================================================


def test_is_docker_available_success():
    mock_res = MagicMock(returncode=0, stdout="Server Version: 24.0.7\n", stderr="")
    with patch("subprocess.run", return_value=mock_res) as mock_run:
        ok, msg = is_docker_available()
        assert ok is True
        assert "running and responsive" in msg
        mock_run.assert_called_once_with(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )


def test_is_docker_available_failure_with_stderr():
    mock_res = MagicMock(
        returncode=1,
        stdout="",
        stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    )
    with patch("subprocess.run", return_value=mock_res):
        ok, msg = is_docker_available()
        assert ok is False
        assert "Docker daemon is not running" in msg
        assert "Cannot connect to the Docker daemon" in msg


def test_is_docker_available_failure_with_stdout():
    mock_res = MagicMock(
        returncode=1,
        stdout="Docker daemon error",
        stderr="",
    )
    with patch("subprocess.run", return_value=mock_res):
        ok, msg = is_docker_available()
        assert ok is False
        assert "Docker daemon is not running: Docker daemon error" in msg


def test_is_docker_available_not_found():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        ok, msg = is_docker_available()
        assert ok is False
        assert "docker command not found in PATH" in msg


def test_is_docker_available_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker info", timeout=10)):
        ok, msg = is_docker_available()
        assert ok is False
        assert "timed out after 10s" in msg


def test_is_docker_available_unexpected_exception():
    with patch("subprocess.run", side_effect=PermissionError("Permission denied")):
        ok, msg = is_docker_available()
        assert ok is False
        assert "Unexpected error checking Docker availability: Permission denied" in msg


# =====================================================================
# stop_staging_db tests
# =====================================================================


def test_stop_staging_db_success():
    stop_res = MagicMock(returncode=0, stdout="my-container\n", stderr="")
    rm_res = MagicMock(returncode=0, stdout="my-container\n", stderr="")
    with patch("subprocess.run", side_effect=[stop_res, rm_res]) as mock_run:
        ok, msg = stop_staging_db("my-container", timeout=10)
        assert ok is True
        assert "stopped and removed successfully" in msg
        assert mock_run.call_count == 2
        mock_run.assert_has_calls(
            [
                call(["docker", "stop", "-t", "10", "my-container"], capture_output=True, text=True, timeout=25),
                call(["docker", "rm", "-f", "-v", "my-container"], capture_output=True, text=True, timeout=30),
            ]
        )


def test_stop_staging_db_empty_name():
    ok, msg = stop_staging_db("   ")
    assert ok is False
    assert "Container name cannot be empty" in msg


def test_stop_staging_db_stop_error_but_rm_success():
    # If stop raises exception or fails, rm should still proceed
    rm_res = MagicMock(returncode=0, stdout="my-container\n", stderr="")
    with patch("subprocess.run", side_effect=[Exception("Stop failed"), rm_res]):
        ok, msg = stop_staging_db("my-container")
        assert ok is True
        assert "stopped and removed successfully" in msg


def test_stop_staging_db_rm_failure():
    stop_res = MagicMock(returncode=0, stdout="my-container\n", stderr="")
    rm_res = MagicMock(returncode=1, stdout="", stderr="Error: No such container: my-container")
    with patch("subprocess.run", side_effect=[stop_res, rm_res]):
        ok, msg = stop_staging_db("my-container")
        assert ok is False
        assert "Failed to remove container 'my-container'" in msg
        assert "No such container" in msg


def test_stop_staging_db_timeout():
    stop_res = MagicMock(returncode=0, stdout="my-container\n", stderr="")
    with patch("subprocess.run", side_effect=[stop_res, subprocess.TimeoutExpired(cmd="docker rm", timeout=30)]):
        ok, msg = stop_staging_db("my-container")
        assert ok is False
        assert "Timed out removing container 'my-container'" in msg


def test_stop_staging_db_not_found():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        ok, msg = stop_staging_db("my-container")
        assert ok is False
        assert "docker command not found in PATH" in msg


def test_stop_staging_db_unexpected_exception():
    stop_res = MagicMock(returncode=0, stdout="my-container\n", stderr="")
    with patch("subprocess.run", side_effect=[stop_res, RuntimeError("Disk failure")]):
        ok, msg = stop_staging_db("my-container")
        assert ok is False
        assert "Unexpected error stopping container 'my-container': Disk failure" in msg


# =====================================================================
# cleanup_orphan_containers tests
# =====================================================================


def test_cleanup_orphan_containers_found_and_removed():
    ps_res = MagicMock(returncode=0, stdout="cid1\ncid2\ncid3\n", stderr="")
    rm_res = MagicMock(returncode=0, stdout="cid1\ncid2\ncid3\n", stderr="")
    with patch("subprocess.run", side_effect=[ps_res, rm_res]) as mock_run:
        count = cleanup_orphan_containers()
        assert count == 3
        mock_run.assert_has_calls(
            [
                call(["docker", "ps", "-aq", "--filter", "label=managed-by=adco-knob-tuner"], capture_output=True, text=True, timeout=30),
                call(["docker", "rm", "-f", "-v", "cid1", "cid2", "cid3"], capture_output=True, text=True, timeout=60),
            ]
        )


def test_cleanup_orphan_containers_none_found():
    ps_res = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=ps_res) as mock_run:
        count = cleanup_orphan_containers()
        assert count == 0
        mock_run.assert_called_once()


def test_cleanup_orphan_containers_ps_failure():
    ps_res = MagicMock(returncode=1, stdout="", stderr="docker error")
    with patch("subprocess.run", return_value=ps_res):
        count = cleanup_orphan_containers()
        assert count == 0


def test_cleanup_orphan_containers_partial_removal_fallback():
    ps_res = MagicMock(returncode=0, stdout="cid1\ncid2\n", stderr="")
    rm_res = MagicMock(returncode=1, stdout="cid1\n", stderr="Error removing cid2")
    with patch("subprocess.run", side_effect=[ps_res, rm_res]):
        count = cleanup_orphan_containers()
        assert count == 1


def test_cleanup_orphan_containers_exception_handling():
    with patch("subprocess.run", side_effect=Exception("Docker crashed")):
        count = cleanup_orphan_containers()
        assert count == 0


# =====================================================================
# start_staging_db tests
# =====================================================================


def test_start_staging_db_invalid_db_type():
    with pytest.raises(ValueError, match="Unsupported db_type 'sqlite'"):
        start_staging_db(db_type="sqlite")


def test_start_staging_db_postgres_success(tmp_path):
    run_res = MagicMock(returncode=0, stdout="container_id_123\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="0.0.0.0:54321\n:::54321\n", stderr="")
    exec_res = MagicMock(returncode=0, stdout="127.0.0.1:5432 - accepting connections\n", stderr="")

    with patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="10.0.0.2\n"), exec_res]) as mock_run, \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("src.knob_tuner.tools.docker_tools.run_safe_query", return_value=[{"?column?": 1}]) as mock_query:

        cname, cfg = start_staging_db(
            db_type="postgres",
            budget=budget(2, 2),
            database="bench_pg",
            timeout=30,
        )

        assert cname.startswith("adco-staging-postgres-")
        assert isinstance(cfg, DBConfig)
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 54321
        assert cfg.user == "postgres"
        assert cfg.password == "postgres"
        assert cfg.database == "bench_pg"
        assert cfg.db_type == "postgres"
        assert cfg.env == "staging"
        assert cfg.restart_type == "docker"
        assert cfg.restart_target == cname

        # Verify docker run command arguments
        run_call_args = mock_run.call_args_list[0][0][0]
        assert "docker" == run_call_args[0]
        assert "run" == run_call_args[1]
        assert "--name" in run_call_args
        assert "--label" in run_call_args
        assert "managed-by=adco-knob-tuner" in run_call_args
        assert "--cpus=2" in run_call_args
        assert "--memory=2g" in run_call_args
        assert "--memory-swap=2g" in run_call_args
        assert "-p" in run_call_args
        assert "127.0.0.1::5432" in run_call_args
        assert "-e" in run_call_args
        assert "POSTGRES_USER=postgres" in run_call_args
        assert "POSTGRES_PASSWORD=postgres" in run_call_args
        assert "POSTGRES_DB=bench_pg" in run_call_args
        assert "-v" not in run_call_args
        assert "postgres:17" in run_call_args

        # Verify safe query was executed
        mock_query.assert_called_once_with(cfg, "SELECT 1")


def test_start_staging_db_postgresql_alias_success():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="127.0.0.1:49153\n", stderr="")
    exec_res = MagicMock(returncode=0, stdout="accepting connections\n", stderr="")

    with patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="10.0.0.2\n"), exec_res]), \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("src.knob_tuner.tools.docker_tools.run_safe_query", return_value=[{"?column?": 1}]):

        cname, cfg = start_staging_db(db_type="postgresql", budget=budget())
        assert cname.startswith("adco-staging-postgres-")
        assert cfg.port == 49153
        assert cfg.db_type == "postgres"


def test_start_staging_db_mysql_success():
    run_res = MagicMock(returncode=0, stdout="container_id_mysql\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="127.0.0.1:33060\n", stderr="")
    exec_res = MagicMock(returncode=0, stdout="mysqld is alive\n", stderr="")

    with patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="10.0.0.2\n"), exec_res]) as mock_run, \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("src.knob_tuner.tools.docker_tools.run_safe_query", return_value=[{"1": 1}]):

        cname, cfg = start_staging_db(
            db_type="mysql",
            budget=budget(1, 1),
            database="mysql_bench",
            timeout=30,
        )

        assert cname.startswith("adco-staging-mysql-")
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 33060
        assert cfg.user == "root"
        assert cfg.password == "mysql_root_password"
        assert cfg.database == "mysql_bench"
        assert cfg.db_type == "mysql"
        assert cfg.env == "staging"
        assert cfg.restart_type == "docker"
        assert cfg.restart_target == cname

        # Verify docker run args
        run_call_args = mock_run.call_args_list[0][0][0]
        assert "127.0.0.1::3306" in run_call_args
        assert "MYSQL_ROOT_PASSWORD=mysql_root_password" in run_call_args
        assert "MYSQL_DATABASE=mysql_bench" in run_call_args
        assert "mysql:8.4" in run_call_args
        assert "--mysql-native-password=ON" in run_call_args


def test_start_staging_db_run_failure():
    run_res = MagicMock(returncode=1, stdout="", stderr="Error: port is already allocated")
    with patch("subprocess.run", return_value=run_res):
        with pytest.raises(RuntimeError, match="Failed to start staging DB container"):
            start_staging_db(db_type="postgres", budget=budget())


def test_start_staging_db_run_exception():
    with patch("subprocess.run", side_effect=Exception("Docker daemon died")):
        with pytest.raises(RuntimeError, match="Failed to execute docker run"):
            start_staging_db(db_type="postgres", budget=budget())


def test_start_staging_db_port_failure():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=1, stdout="", stderr="Error inspecting port")
    with patch("subprocess.run", side_effect=[run_res, port_res]), \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("src.knob_tuner.tools.docker_tools.stop_staging_db") as mock_stop:
        with pytest.raises(RuntimeError, match="Failed to get port mapping"):
            start_staging_db(db_type="postgres", budget=budget())
        mock_stop.assert_called_once()


def test_start_staging_db_port_unparseable():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="invalid_port_output\n", stderr="")
    with patch("subprocess.run", side_effect=[run_res, port_res]), \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("src.knob_tuner.tools.docker_tools.stop_staging_db") as mock_stop:
        with pytest.raises(RuntimeError, match="Failed to parse mapped host port"):
            start_staging_db(db_type="postgres", budget=budget())
        mock_stop.assert_called_once()


def test_start_staging_db_readiness_timeout():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="127.0.0.1:54321\n", stderr="")
    exec_res = MagicMock(returncode=1, stdout="", stderr="not ready")
    logs_res = MagicMock(returncode=0, stdout="FATAL: database not ready\n", stderr="")

    # Time simulation: simulate timeout expiring quickly
    with patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="10.0.0.2\n"), exec_res, exec_res, exec_res, logs_res]), \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("time.time", side_effect=[100.0, 100.0, 101.0, 102.0, 110.0]), \
         patch("time.sleep"), \
         patch("src.knob_tuner.tools.docker_tools.stop_staging_db") as mock_stop:

        with pytest.raises(TimeoutError, match="did not become ready within 5s") as exc_info:
            start_staging_db(db_type="postgres", budget=budget(), timeout=5)

        assert "FATAL: database not ready" in str(exc_info.value)
        mock_stop.assert_called_once()


def test_start_staging_db_readiness_timeout_with_container_logs():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="127.0.0.1:54321\n", stderr="")
    exec_res = MagicMock(returncode=1, stdout="", stderr="connection refused")
    logs_res = MagicMock(
        returncode=0,
        stdout="2026-09-07 [LOG] starting PostgreSQL 17.2\n2026-09-07 [ERROR] syntax error in postgresql.conf\n",
        stderr="sh: /docker-entrypoint-initdb.d/01.sh: Permission denied",
    )

    with patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="10.0.0.2\n"), exec_res, logs_res]), \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("time.time", side_effect=[100.0, 100.0, 140.0]), \
         patch("time.sleep"), \
         patch("src.knob_tuner.tools.docker_tools.stop_staging_db") as mock_stop:

        with pytest.raises(TimeoutError) as exc_info:
            start_staging_db(db_type="postgres", budget=budget(), timeout=30)

        err_text = str(exc_info.value)
        assert "did not become ready within 30s" in err_text
        assert "Container logs (tail 50):" in err_text
        assert "syntax error in postgresql.conf" in err_text
        assert "Permission denied" in err_text
        mock_stop.assert_called_once()


def test_start_staging_db_readiness_timeout_log_fetch_exception():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="127.0.0.1:54321\n", stderr="")
    exec_res = MagicMock(returncode=1, stdout="", stderr="connection refused")

    with patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="10.0.0.2\n"), exec_res, Exception("docker logs failed")]), \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("time.time", side_effect=[100.0, 100.0, 140.0]), \
         patch("time.sleep"), \
         patch("src.knob_tuner.tools.docker_tools.stop_staging_db") as mock_stop:

        with pytest.raises(TimeoutError) as exc_info:
            start_staging_db(db_type="postgres", budget=budget(), timeout=30)

        err_text = str(exc_info.value)
        assert "did not become ready within 30s" in err_text
        assert "Failed to retrieve container logs: docker logs failed" in err_text
        mock_stop.assert_called_once()


def test_start_staging_db_query_verification_retry_then_success():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="127.0.0.1:54321\n", stderr="")
    exec_res1 = MagicMock(returncode=0, stdout="accepting connections", stderr="")
    exec_res2 = MagicMock(returncode=0, stdout="accepting connections", stderr="")

    # First query attempt fails (cfg and fallback_cfg), second succeeds (cfg)
    with patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="10.0.0.2\n"), exec_res1, exec_res2]), \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("time.sleep"), \
         patch("src.knob_tuner.tools.docker_tools.run_safe_query", side_effect=[Exception("Refused"), Exception("Refused"), [{"1": 1}]]):

        cname, cfg = start_staging_db(db_type="postgres", budget=budget(), timeout=10)
        assert cname.startswith("adco-staging-postgres-")
        assert cfg.port == 54321


# =====================================================================
# resolve_docker_image tests
# =====================================================================


def test_resolve_docker_image_unsupported_type():
    with pytest.raises(ValueError, match="Unsupported db_type 'oracle'"):
        resolve_docker_image("oracle")


def test_resolve_docker_image_postgres_defaults():
    assert resolve_docker_image("postgres") == "postgres:17"
    assert resolve_docker_image("postgres", None) == "postgres:17"
    assert resolve_docker_image("postgresql", "") == "postgres:17"
    assert resolve_docker_image("POSTGRES", "   ") == "postgres:17"


def test_resolve_docker_image_postgres_raw_versions():
    assert resolve_docker_image("postgres", "16") == "postgres:16"
    assert resolve_docker_image("postgres", "16.3") == "postgres:16.3"
    assert resolve_docker_image("postgresql", "15.4") == "postgres:15.4"


def test_resolve_docker_image_postgres_banners():
    banner1 = "PostgreSQL 16.3 on x86_64-pc-linux-gnu, compiled by gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0, 64-bit"
    assert resolve_docker_image("postgres", banner1) == "postgres:16.3"

    banner2 = "PostgreSQL 17.2 (Debian 17.2-1.pgdg120+1) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 12.2.0-14) 12.2.0, 64-bit"
    assert resolve_docker_image("postgresql", banner2) == "postgres:17.2"

    banner3 = "PostgreSQL 15"
    assert resolve_docker_image("postgres", banner3) == "postgres:15"

    banner4 = "PostgreSQL 17.2 (Homebrew)"
    assert resolve_docker_image("postgres", banner4) == "postgres:17.2"

    banner5 = "PostgreSQL 16.3 (Debian 16.3-1.pgdg120+1)"
    assert resolve_docker_image("postgres", banner5) == "postgres:16.3"

    banner6 = "17.4-1.pgdg120+2"
    assert resolve_docker_image("postgres", banner6) == "postgres:17.4"

    banner7 = "PostgreSQL 17.4-1.pgdg120+2"
    assert resolve_docker_image("postgres", banner7) == "postgres:17.4"

    banner8 = "PostgreSQL 17.2 (Homebrew) on aarch64-apple-darwin24.2.0"
    assert resolve_docker_image("postgres", banner8) == "postgres:17.2"

    banner9 = "PostgreSQL 17.11 (Debian 17.11-1.pgdg13+2) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit"
    assert resolve_docker_image("postgres", banner9) == "postgres:17"


def test_resolve_docker_image_postgres_custom_tags():
    assert resolve_docker_image("postgres", "postgres:16") == "postgres:16"
    assert resolve_docker_image("postgres", "postgres:16.3") == "postgres:16.3"
    assert resolve_docker_image("postgres", "postgresql:15") == "postgres:15"
    assert resolve_docker_image("postgres", "postgres:16-alpine") == "postgres:16-alpine"
    assert resolve_docker_image("postgres", "16-alpine") == "postgres:16-alpine"


def test_resolve_docker_image_mysql_defaults():
    assert resolve_docker_image("mysql") == "mysql:8.4"
    assert resolve_docker_image("mysql", None) == "mysql:8.4"
    assert resolve_docker_image("MYSQL", "") == "mysql:8.4"
    assert resolve_docker_image("mysql", "   ") == "mysql:8.4"


def test_resolve_docker_image_mysql_raw_and_patch_versions():
    assert resolve_docker_image("mysql", "8.4") == "mysql:8.4"
    assert resolve_docker_image("mysql", "8.0") == "mysql:8.0"
    assert resolve_docker_image("mysql", "8.4.0") == "mysql:8.4"
    assert resolve_docker_image("mysql", "8.0.35") == "mysql:8.0"
    assert resolve_docker_image("mysql", "5.7.44") == "mysql:5.7"


def test_resolve_docker_image_mysql_banners():
    assert resolve_docker_image("mysql", "8.0.35-0ubuntu0.22.04.1") == "mysql:8.0"
    assert resolve_docker_image("mysql", "MySQL Community Server - GPL 8.0.35") == "mysql:8.0"
    assert resolve_docker_image("mysql", "8.0.36 MySQL Community Server") == "mysql:8.0"
    assert resolve_docker_image("mysql", "8.4.0-commercial") == "mysql:8.4"
    assert resolve_docker_image("mysql", "MySQL 8.4 (Homebrew)") == "mysql:8.4"


def test_resolve_docker_image_mysql_custom_tags():
    assert resolve_docker_image("mysql", "mysql:8.0") == "mysql:8.0"
    assert resolve_docker_image("mysql", "mysql:8.4.0") == "mysql:8.4"


# =====================================================================
# start_staging_db with db_version tests
# =====================================================================


def test_start_staging_db_postgres_with_version():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="127.0.0.1:54321\n", stderr="")
    exec_res = MagicMock(returncode=0, stdout="accepting connections\n", stderr="")

    with patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="10.0.0.2\n"), exec_res]) as mock_run, \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("src.knob_tuner.tools.docker_tools.run_safe_query", return_value=[{"1": 1}]):

        cname, cfg = start_staging_db(db_type="postgres", db_version="16", budget=budget())
        assert cname.startswith("adco-staging-postgres-")
        assert cfg.port == 54321

        run_call_args = mock_run.call_args_list[0][0][0]
        assert "postgres:16" in run_call_args


def test_start_staging_db_postgres_with_banner():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="127.0.0.1:54321\n", stderr="")
    exec_res = MagicMock(returncode=0, stdout="accepting connections\n", stderr="")

    banner = "PostgreSQL 16.3 on x86_64-pc-linux-gnu, compiled by gcc"
    with patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="10.0.0.2\n"), exec_res]) as mock_run, \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("src.knob_tuner.tools.docker_tools.run_safe_query", return_value=[{"1": 1}]):

        cname, cfg = start_staging_db(db_type="postgres", db_version=banner, budget=budget())
        assert cname.startswith("adco-staging-postgres-")

        run_call_args = mock_run.call_args_list[0][0][0]
        assert "postgres:16.3" in run_call_args


def test_start_staging_db_mysql_with_version():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="127.0.0.1:33060\n", stderr="")
    exec_res = MagicMock(returncode=0, stdout="mysqld is alive\n", stderr="")

    with patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="10.0.0.2\n"), exec_res]) as mock_run, \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("src.knob_tuner.tools.docker_tools.run_safe_query", return_value=[{"1": 1}]):

        cname, cfg = start_staging_db(db_type="mysql", db_version="8.0", budget=budget())
        assert cname.startswith("adco-staging-mysql-")

        run_call_args = mock_run.call_args_list[0][0][0]
        assert "mysql:8.0" in run_call_args


def test_start_staging_db_mysql_with_banner():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="127.0.0.1:33060\n", stderr="")
    exec_res = MagicMock(returncode=0, stdout="mysqld is alive\n", stderr="")

    banner = "8.0.35-0ubuntu0.22.04.1"
    with patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="10.0.0.2\n"), exec_res]) as mock_run, \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("src.knob_tuner.tools.docker_tools.run_safe_query", return_value=[{"1": 1}]):

        cname, cfg = start_staging_db(db_type="mysql", db_version=banner, budget=budget())
        assert cname.startswith("adco-staging-mysql-")

        run_call_args = mock_run.call_args_list[0][0][0]
        assert "mysql:8.0" in run_call_args


# =====================================================================
# restart_docker_db and recreate_docker_db tests
# =====================================================================

def test_restart_docker_db_success():
    mock_res = MagicMock(returncode=0, stdout="postgres_db\n", stderr="")
    with patch("subprocess.run", return_value=mock_res):
        from src.knob_tuner.tools.docker_tools import restart_docker_db
        ok, msg = restart_docker_db("postgres_db")
        assert ok is True
        assert "restarted and ready" in msg

def test_restart_docker_db_failure():
    mock_res = MagicMock(
        returncode=1, stdout="", stderr="Error: No such container: bad_container"
    )
    with patch("subprocess.run", return_value=mock_res):
        from src.knob_tuner.tools.docker_tools import restart_docker_db
        ok, msg = restart_docker_db("bad_container")
        assert ok is False
        assert "Failed to restart container 'bad_container'" in msg
        assert "No such container" in msg

def test_restart_docker_db_empty_name():
    from src.knob_tuner.tools.docker_tools import restart_docker_db
    ok, msg = restart_docker_db("   ")
    assert ok is False
    assert "Container name cannot be empty" in msg

def test_recreate_docker_db_success():
    new_cfg = DBConfig(host="10.0.0.1", port=5555, user="u", password="p", database="d", db_type="postgres", env="staging")
    with patch("src.knob_tuner.tools.docker_tools.stop_staging_db") as mock_stop, \
         patch("src.knob_tuner.tools.docker_tools.start_staging_db", return_value=("new-container", new_cfg)) as mock_start:
        from src.knob_tuner.tools.docker_tools import recreate_docker_db
        b = budget(4, 8)
        ok, cname, cfg = recreate_docker_db("old-container", budget=b)
        assert ok is True
        assert cname == "new-container"
        assert cfg == new_cfg
        mock_stop.assert_called_once_with("old-container")
        assert mock_start.call_args.kwargs["budget"] is b

def test_recreate_docker_db_failure():
    with patch("src.knob_tuner.tools.docker_tools.stop_staging_db"), \
         patch("src.knob_tuner.tools.docker_tools.start_staging_db", side_effect=Exception("Failed to start")):
        from src.knob_tuner.tools.docker_tools import recreate_docker_db
        ok, err, cfg = recreate_docker_db("old-container", budget=budget())
        assert ok is False
        assert "Failed to start" in err
        assert cfg is None


def test_recreate_docker_db_requires_budget():
    with patch("src.knob_tuner.tools.docker_tools.start_staging_db") as mock_start:
        from src.knob_tuner.tools.docker_tools import recreate_docker_db
        with pytest.raises(ValueError, match="ResourceBudget is required to recreate"):
            recreate_docker_db("old-container")
        mock_start.assert_not_called()

# =====================================================================
# get_container_host_port tests
# =====================================================================

def test_get_container_host_port_success():
    mock_res = MagicMock(returncode=0, stdout="0.0.0.0:54321\n:::54321\n", stderr="")
    with patch("subprocess.run", return_value=mock_res) as mock_run:
        port = get_container_host_port("my-container", 5432)
        assert port == 54321
        mock_run.assert_called_once_with(
            ["docker", "port", "my-container", "5432"],
            capture_output=True,
            text=True,
            timeout=15,
        )

def test_get_container_host_port_failure():
    mock_res = MagicMock(returncode=1, stdout="", stderr="Error: No such container: my-container")
    with patch("subprocess.run", return_value=mock_res):
        with pytest.raises(RuntimeError, match="Failed to get port mapping for container 'my-container'"):
            get_container_host_port("my-container", 5432)

def test_get_container_host_port_malformed():
    mock_res = MagicMock(returncode=0, stdout="invalid_output\n", stderr="")
    with patch("subprocess.run", return_value=mock_res):
        with pytest.raises(RuntimeError, match="Failed to parse mapped host port from docker port output: 'invalid_output'"):
            get_container_host_port("my-container", 5432)

def test_get_container_host_port_exception():
    with patch("subprocess.run", side_effect=Exception("Docker daemon died")):
        with pytest.raises(Exception, match="Docker daemon died"):
            get_container_host_port("my-container", 5432)


def test_get_current_docker_network_env_override():
    from src.knob_tuner.tools.docker_tools import get_current_docker_network
    with patch.dict(os.environ, {"ADCO_DOCKER_NETWORK": "custom_net"}):
        assert get_current_docker_network() == "custom_net"


def test_get_current_docker_network_inside_container():
    from src.knob_tuner.tools.docker_tools import get_current_docker_network
    mock_res = MagicMock(returncode=0, stdout="bridge adco-experiments_default\n")
    with patch.dict(os.environ, {}, clear=True), \
         patch("os.path.exists", return_value=True), \
         patch("subprocess.run", return_value=mock_res):
        assert get_current_docker_network() == "adco-experiments_default"


def test_start_staging_db_with_custom_network():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="127.0.0.1:54321\n", stderr="")
    exec_res = MagicMock(returncode=0, stdout="accepting connections", stderr="")

    with patch("src.knob_tuner.tools.docker_tools.get_current_docker_network", return_value="my_compose_net"), \
         patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="172.18.0.5\n"), exec_res]) as mock_run, \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("src.knob_tuner.tools.docker_tools.run_safe_query", return_value=[{"1": 1}]):

        cname, cfg = start_staging_db(db_type="postgres", budget=budget())
        assert cfg.host == cname
        assert cfg.port == 5432
        # Verify --network my_compose_net was passed to docker run
        run_cmd = mock_run.call_args_list[0][0][0]
        assert "--network" in run_cmd
        assert run_cmd[run_cmd.index("--network") + 1] == "my_compose_net"


# =====================================================================
# ResourceBudget enforcement tests
# =====================================================================


def test_start_staging_db_requires_budget():
    with patch("subprocess.run") as mock_run:
        with pytest.raises(ValueError, match="ResourceBudget is required to start"):
            start_staging_db(db_type="postgres")
        mock_run.assert_not_called()


def test_start_staging_db_uses_budget_for_limits():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    port_res = MagicMock(returncode=0, stdout="127.0.0.1:54321\n", stderr="")
    exec_res = MagicMock(returncode=0, stdout="accepting connections", stderr="")

    b = budget(4, 8)
    with patch("subprocess.run", side_effect=[run_res, port_res, MagicMock(returncode=0, stdout="10.0.0.2\n"), exec_res]) as mock_run, \
         patch("src.knob_tuner.tools.docker_tools.verify_container_resources", return_value=VERIFY_OK), \
         patch("src.knob_tuner.tools.docker_tools.run_safe_query", return_value=[{"1": 1}]):

        start_staging_db(db_type="postgres", budget=b)

        run_cmd = mock_run.call_args_list[0][0][0]
        assert "--cpus=4" in run_cmd
        assert "--memory=8g" in run_cmd
        assert "--memory-swap=8g" in run_cmd


def test_start_staging_db_stops_container_when_verification_fails():
    run_res = MagicMock(returncode=0, stdout="cid\n", stderr="")
    with patch("subprocess.run", side_effect=[run_res]), \
         patch(
             "src.knob_tuner.tools.docker_tools.verify_container_resources",
             return_value=(False, "Container 'x' resource mismatch: cpus actual=1 expected=2"),
         ), \
         patch("src.knob_tuner.tools.docker_tools.stop_staging_db") as mock_stop:

        with pytest.raises(RuntimeError, match="resource mismatch"):
            start_staging_db(db_type="postgres", budget=budget())
        mock_stop.assert_called_once()


# =====================================================================
# verify_container_resources tests
# =====================================================================


def test_verify_container_resources_match():
    b = budget(4, 8)
    inspect_res = MagicMock(
        returncode=0, stdout="4000000000 8589934592\n", stderr=""
    )
    with patch("subprocess.run", return_value=inspect_res) as mock_run:
        ok, msg = verify_container_resources("my-container", b)
        assert ok is True
        assert "verified" in msg
        mock_run.assert_called_once_with(
            [
                "docker",
                "inspect",
                "--format",
                "{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}",
                "my-container",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )


def test_verify_container_resources_cpu_mismatch():
    b = budget(4, 8)
    inspect_res = MagicMock(
        returncode=0, stdout="2000000000 8589934592\n", stderr=""
    )
    with patch("subprocess.run", return_value=inspect_res):
        ok, msg = verify_container_resources("my-container", b)
        assert ok is False
        assert "cpus actual=2000000000 expected=4000000000" in msg
        assert "memory" not in msg


def test_verify_container_resources_memory_mismatch():
    b = budget(4, 8)
    inspect_res = MagicMock(
        returncode=0, stdout="4000000000 1073741824\n", stderr=""
    )
    with patch("subprocess.run", return_value=inspect_res):
        ok, msg = verify_container_resources("my-container", b)
        assert ok is False
        assert "memory actual=1073741824 expected=8589934592" in msg
        assert "cpus" not in msg


def test_verify_container_resources_both_mismatch():
    b = budget(2, 2)
    inspect_res = MagicMock(returncode=0, stdout="0 0\n", stderr="")
    with patch("subprocess.run", return_value=inspect_res):
        ok, msg = verify_container_resources("my-container", b)
        assert ok is False
        assert "cpus actual=0 expected=2000000000" in msg
        assert "memory actual=0 expected=2147483648" in msg


def test_verify_container_resources_malformed_output():
    b = budget(4, 8)
    inspect_res = MagicMock(returncode=0, stdout="not-a-number\n", stderr="")
    with patch("subprocess.run", return_value=inspect_res):
        ok, msg = verify_container_resources("my-container", b)
        assert ok is False
        assert "Malformed docker inspect output" in msg


def test_verify_container_resources_inspect_failure():
    b = budget(4, 8)
    inspect_res = MagicMock(
        returncode=1, stdout="", stderr="Error: No such container: my-container"
    )
    with patch("subprocess.run", return_value=inspect_res):
        ok, msg = verify_container_resources("my-container", b)
        assert ok is False
        assert "Failed to inspect container" in msg
        assert "No such container" in msg


def test_verify_container_resources_subprocess_exception():
    b = budget(4, 8)
    with patch("subprocess.run", side_effect=Exception("Docker daemon died")):
        ok, msg = verify_container_resources("my-container", b)
        assert ok is False
        assert "Docker daemon died" in msg


