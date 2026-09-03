"""Unit tests for benchmark_tools module."""

import os
from unittest.mock import MagicMock, patch
import pytest
from src.knob_tuner.tools import run_sysbench_benchmark
from src.knob_tuner.tools.benchmark_tools import _parse_sysbench_summary
from src.knob_tuner.tools.db_connector import DBConfig


SAMPLE_SYSBENCH_OUTPUT = """
sysbench 1.0.20 (using bundled LuaJIT 2.1.0-beta3)

Running the test with following options:
Number of threads: 32
Report intermediate results: every 60 second(s)
Initializing random number generator from current time


Initializing worker threads...

Threads started!

[ 60s ] thds: 32 tps: 1200.50 qps: 24010.00 (r/w/o: 16807.00/4802.00/2401.00) lat (ms,95%): 34.20 err/s: 0.00 reconn/s: 0.00
[ 120s ] thds: 32 tps: 1300.50 qps: 26010.00 (r/w/o: 18207.00/5202.00/2601.00) lat (ms,95%): 32.10 err/s: 0.00 reconn/s: 0.00

SQL statistics:
    queries performed:
        read:                            2041000
        write:                           583100
        other:                           291500
        total:                           2915600
    transactions:                        145780 (1214.83 per sec.)
    queries:                             2915600 (24296.67 per sec.)
    ignored errors:                      0      (0.00 per sec.)
    reconnects:                          0      (0.00 per sec.)

General statistics:
    total time:                          120.0012s
    total number of events:              145780

Latency (ms):
         min:                                    1.15
         avg:                                   26.34
         max:                                  142.50
         95th percentile:                       33.15
         sum:                               3840000.00

Threads fairness:
    events (avg/stddev):           4555.6250/50.12
    execution time (avg/stddev):   119.9500/0.03
"""


def test_export_import():
    from src.knob_tuner.tools import run_sysbench_benchmark as fn
    assert callable(fn)


def test_parse_sysbench_summary():
    details = _parse_sysbench_summary(SAMPLE_SYSBENCH_OUTPUT)
    assert details["total_queries"] == 2915600
    assert details["total_transactions"] == 145780
    assert details["read_queries"] == 2041000
    assert details["write_queries"] == 583100
    assert details["other_queries"] == 291500
    assert details["latency_min_ms"] == 1.15
    assert details["latency_avg_ms"] == 26.34
    assert details["latency_max_ms"] == 142.50
    assert details["latency_95th_ms"] == 33.15
    assert details["latency_sum_ms"] == 3840000.00
    assert details["total_events"] == 145780


def test_run_sysbench_benchmark_postgres_tables_exist(mock_db_config_pg, tmp_path):
    # Mock DB connection returning 50 tables (so count == tables, no prepare needed)
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = (50,)

    workdir = str(tmp_path / "logs")

    with patch("src.knob_tuner.tools.benchmark_tools.get_connection", return_value=mock_conn), \
         patch("src.knob_tuner.tools.benchmark_tools.subprocess.run") as mock_run:

        # subprocess.run for sysbench run
        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_SYSBENCH_OUTPUT, stderr="")

        res = run_sysbench_benchmark(
            cfg=mock_db_config_pg,
            tables=50,
            table_size=100000,
            threads=32,
            duration=120,
            report_interval=60,
            workdir=workdir,
        )

        assert res["status"] == "ok"
        assert res["duration"] == 120
        assert res["threads"] == 32
        assert res["tables"] == 50
        assert res["error"] is None
        # QPS is average of last 2 intervals: (24010.00 + 26010.00) / 2 = 25010.00
        assert res["qps"] == 25010.00
        assert res["tps"] == 25010.00 / 20.0
        assert os.path.isfile(res["log_file"])

        # Check that check_query was run for postgres
        mock_cursor.execute.assert_called_once()
        query_arg = mock_cursor.execute.call_args[0][0]
        assert "public" in query_arg
        assert "sbtest%" in query_arg

        # subprocess.run should be called once (only run, no cleanup or prepare)
        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert "--db-driver=pgsql" in cmd
        assert f"--pgsql-host={mock_db_config_pg.host}" in cmd
        assert f"--pgsql-port={mock_db_config_pg.port}" in cmd
        assert f"--pgsql-user={mock_db_config_pg.user}" in cmd
        assert f"--pgsql-password={mock_db_config_pg.password}" in cmd
        assert f"--pgsql-db={mock_db_config_pg.database}" in cmd
        assert "--tables=50" in cmd
        assert "--table-size=100000" in cmd
        assert "--threads=32" in cmd
        assert "--time=120" in cmd
        assert "--report-interval=60" in cmd
        assert "run" in cmd


def test_run_sysbench_benchmark_mysql_needs_prepare(mock_db_config_mysql, tmp_path):
    # Mock DB connection returning 0 tables (needs prepare)
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = {"count(*)": 0}  # dict response from DictCursor

    workdir = str(tmp_path / "mysql_logs")

    with patch("src.knob_tuner.tools.benchmark_tools.get_connection", return_value=mock_conn), \
         patch("src.knob_tuner.tools.benchmark_tools.subprocess.run") as mock_run:

        # 1st call: cleanup, 2nd call: prepare, 3rd call: run
        cleanup_res = MagicMock(returncode=0, stdout="", stderr="")
        prepare_res = MagicMock(returncode=0, stdout="Prepare completed", stderr="")
        run_res = MagicMock(returncode=0, stdout=SAMPLE_SYSBENCH_OUTPUT, stderr="")
        mock_run.side_effect = [cleanup_res, prepare_res, run_res]

        res = run_sysbench_benchmark(
            cfg=mock_db_config_mysql,
            tables=10,
            table_size=50000,
            threads=16,
            duration=60,
            report_interval=60,
            workdir=workdir,
        )

        assert res["status"] == "ok"
        assert res["duration"] == 60
        assert res["threads"] == 16
        assert res["tables"] == 10
        assert mock_run.call_count == 3

        # Check cleanup cmd
        cleanup_cmd = mock_run.call_args_list[0][0][0]
        assert "--db-driver=mysql" in cleanup_cmd
        assert f"--mysql-host={mock_db_config_mysql.host}" in cleanup_cmd
        assert f"--mysql-db={mock_db_config_mysql.database}" in cleanup_cmd
        assert "cleanup" in cleanup_cmd

        # Check prepare cmd and prepare log file
        prepare_cmd = mock_run.call_args_list[1][0][0]
        assert "prepare" in prepare_cmd
        prepare_log = os.path.join(workdir, "sysbench_prepare.log")
        assert os.path.isfile(prepare_log)
        with open(prepare_log) as f:
            assert "Prepare completed" in f.read()

        # Check run cmd
        run_cmd = mock_run.call_args_list[2][0][0]
        assert "run" in run_cmd


def test_run_sysbench_benchmark_prepare_failure(mock_db_config_mysql, tmp_path):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = (0,)

    workdir = str(tmp_path / "fail_logs")

    with patch("src.knob_tuner.tools.benchmark_tools.get_connection", return_value=mock_conn), \
         patch("src.knob_tuner.tools.benchmark_tools.subprocess.run") as mock_run:

        cleanup_res = MagicMock(returncode=0, stdout="", stderr="")
        prepare_res = MagicMock(returncode=1, stdout="", stderr="FATAL: Disk full")
        mock_run.side_effect = [cleanup_res, prepare_res]

        res = run_sysbench_benchmark(
            cfg=mock_db_config_mysql,
            tables=10,
            workdir=workdir,
        )

        assert res["status"] == "error"
        assert "sysbench prepare failed" in res["error"]
        assert res["tps"] == 0.0
        assert res["qps"] == 0.0


def test_run_sysbench_benchmark_run_failure(mock_db_config_pg, tmp_path):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = (50,)

    workdir = str(tmp_path / "fail_logs")

    with patch("src.knob_tuner.tools.benchmark_tools.get_connection", return_value=mock_conn), \
         patch("src.knob_tuner.tools.benchmark_tools.subprocess.run") as mock_run:

        mock_run.return_value = MagicMock(returncode=127, stdout="", stderr="sysbench: command not found")

        res = run_sysbench_benchmark(
            cfg=mock_db_config_pg,
            tables=50,
            workdir=workdir,
        )

        assert res["status"] == "error"
        assert "sysbench run failed" in res["error"]
        assert res["tps"] == 0.0
        assert res["qps"] == 0.0


def test_run_sysbench_benchmark_unsupported_db_type(tmp_path):
    cfg = DBConfig(
        host="localhost",
        port=1521,
        user="oracle",
        password="pwd",
        database="orcl",
        db_type="oracle",
        env="dev",
    )
    res = run_sysbench_benchmark(cfg=cfg, workdir=str(tmp_path))
    assert res["status"] == "error"
    assert "Unsupported db_type 'oracle'" in res["error"]


def test_run_sysbench_benchmark_db_conn_failure(mock_db_config_pg, tmp_path):
    with patch("src.knob_tuner.tools.benchmark_tools.get_connection", side_effect=Exception("Connection refused")):
        res = run_sysbench_benchmark(cfg=mock_db_config_pg, workdir=str(tmp_path))
        assert res["status"] == "error"
        assert "Connection refused" in res["error"]
        assert res["tps"] == 0.0
        assert res["qps"] == 0.0


def test_run_sysbench_benchmark_summary_fallback_when_no_qps_lines(mock_db_config_pg, tmp_path):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = (50,)

    summary_only_output = """
SQL statistics:
    queries performed:
        read:                            1000
        write:                           200
        other:                           100
        total:                           1300
    transactions:                        100    (50.00 per sec.)
    queries:                             1300   (650.00 per sec.)

Latency (ms):
         min:                                    1.00
         avg:                                    5.00
         max:                                   20.00
         95th percentile:                       10.00
"""
    with patch("src.knob_tuner.tools.benchmark_tools.get_connection", return_value=mock_conn), \
         patch("src.knob_tuner.tools.benchmark_tools.subprocess.run") as mock_run:

        mock_run.return_value = MagicMock(returncode=0, stdout=summary_only_output, stderr="")

        res = run_sysbench_benchmark(
            cfg=mock_db_config_pg,
            tables=50,
            duration=2,
            report_interval=60,
            workdir=str(tmp_path),
        )

        assert res["status"] == "ok"
        assert res["qps"] == 650.00
        assert res["tps"] == 650.00 / 20.0
        assert res["details"]["latency_min_ms"] == 1.00


def test_run_sysbench_benchmark_host_normalization_and_env(tmp_path):
    cfg_localhost = DBConfig(
        host="localhost",
        port=5432,
        user="postgres",
        password="test_secret_password",
        database="testdb",
        db_type="postgres",
        env="staging",
    )
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = (10,)

    with patch("src.knob_tuner.tools.benchmark_tools.get_connection", return_value=mock_conn), \
         patch("src.knob_tuner.tools.benchmark_tools.subprocess.run") as mock_run:

        mock_run.return_value = MagicMock(returncode=0, stdout=SAMPLE_SYSBENCH_OUTPUT, stderr="")

        res = run_sysbench_benchmark(cfg=cfg_localhost, workdir=str(tmp_path))

        assert res["status"] == "ok"
        assert res["tables"] == 10
        assert res["threads"] == 4

        # Verify host normalized to 127.0.0.1
        cmd = mock_run.call_args[0][0]
        assert "--pgsql-host=127.0.0.1" in cmd
        assert "--tables=10" in cmd
        assert "--threads=4" in cmd

        # Verify env has PGPASSWORD and MYSQL_PWD
        call_env = mock_run.call_args[1].get("env", {})
        assert call_env.get("PGPASSWORD") == "test_secret_password"
        assert call_env.get("MYSQL_PWD") == "test_secret_password"


def test_run_sysbench_benchmark_prepare_segfault_recovery(mock_db_config_pg, tmp_path):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = (0,)  # Needs prepare

    with patch("src.knob_tuner.tools.benchmark_tools.get_connection", return_value=mock_conn), \
         patch("src.knob_tuner.tools.benchmark_tools.subprocess.run") as mock_run:

        cleanup_res = MagicMock(returncode=0, stdout="", stderr="")
        prepare_crash = MagicMock(returncode=-11, stdout="", stderr="Segmentation fault (core dumped)")
        prepare_retry_ok = MagicMock(returncode=0, stdout="Prepare retry ok", stderr="")
        run_ok = MagicMock(returncode=0, stdout=SAMPLE_SYSBENCH_OUTPUT, stderr="")

        mock_run.side_effect = [cleanup_res, prepare_crash, prepare_retry_ok, run_ok]

        res = run_sysbench_benchmark(cfg=mock_db_config_pg, tables=10, workdir=str(tmp_path))

        assert res["status"] == "ok"
        assert mock_run.call_count == 4
        # Check that retry prepare used threads=1 and host=127.0.0.1
        retry_prep_cmd = mock_run.call_args_list[2][0][0]
        assert "--threads=1" in retry_prep_cmd
        assert "--pgsql-host=127.0.0.1" in retry_prep_cmd


def test_run_sysbench_benchmark_prepare_segfault_permanent_failure(mock_db_config_pg, tmp_path):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = (0,)

    with patch("src.knob_tuner.tools.benchmark_tools.get_connection", return_value=mock_conn), \
         patch("src.knob_tuner.tools.benchmark_tools.subprocess.run") as mock_run:

        cleanup_res = MagicMock(returncode=0, stdout="", stderr="")
        prepare_crash = MagicMock(returncode=-11, stdout="", stderr="Segmentation fault")
        prepare_retry_crash = MagicMock(returncode=-11, stdout="", stderr="Segmentation fault on retry")

        mock_run.side_effect = [cleanup_res, prepare_crash, prepare_retry_crash]

        res = run_sysbench_benchmark(cfg=mock_db_config_pg, tables=10, workdir=str(tmp_path))

        assert res["status"] == "error"
        assert "Sysbench encountered a segmentation fault (exit code -11)" in res["error"]
        assert "prepare" in res["error"]


def test_run_sysbench_benchmark_run_segfault_recovery(mock_db_config_pg, tmp_path):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = (10,)

    with patch("src.knob_tuner.tools.benchmark_tools.get_connection", return_value=mock_conn), \
         patch("src.knob_tuner.tools.benchmark_tools.subprocess.run") as mock_run:

        run_crash = MagicMock(returncode=139, stdout="", stderr="Segmentation fault (exit code 139)")
        run_retry_ok = MagicMock(returncode=0, stdout=SAMPLE_SYSBENCH_OUTPUT, stderr="")

        mock_run.side_effect = [run_crash, run_retry_ok]

        res = run_sysbench_benchmark(cfg=mock_db_config_pg, tables=10, threads=4, workdir=str(tmp_path))

        assert res["status"] == "ok"
        assert res["threads"] == 1  # updated after safe single-thread retry
        assert mock_run.call_count == 2
        retry_run_cmd = mock_run.call_args_list[1][0][0]
        assert "--threads=1" in retry_run_cmd
        assert "--pgsql-host=127.0.0.1" in retry_run_cmd


def test_run_sysbench_benchmark_run_segfault_permanent_failure(mock_db_config_pg, tmp_path):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchone.return_value = (10,)

    with patch("src.knob_tuner.tools.benchmark_tools.get_connection", return_value=mock_conn), \
         patch("src.knob_tuner.tools.benchmark_tools.subprocess.run") as mock_run:

        run_crash = MagicMock(returncode=-11, stdout="", stderr="Segmentation fault")
        run_retry_crash = MagicMock(returncode=-11, stdout="", stderr="Segmentation fault on retry")

        mock_run.side_effect = [run_crash, run_retry_crash]

        res = run_sysbench_benchmark(cfg=mock_db_config_pg, tables=10, workdir=str(tmp_path))

        assert res["status"] == "error"
        assert "Sysbench encountered a segmentation fault (exit code -11)" in res["error"]
        assert "benchmark run" in res["error"]

