"""Unit tests for benchmark_tools module."""

import os
import subprocess
from unittest.mock import MagicMock, patch

from src.knob_tuner.contracts import SysbenchProfile
from src.knob_tuner.tools import run_sysbench_benchmark
from src.knob_tuner.tools.benchmark_tools import (
    _parse_run_metrics,
    _parse_sysbench_summary,
    run_sysbench_measurement,
)
from src.knob_tuner.tools.db_connector import DBConfig

RUN_PATCH = "src.knob_tuner.tools.benchmark_tools.subprocess.run"
DB_CONN_PATCH = "src.knob_tuner.tools.db_connector.get_connection"


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


def _output_with(
    tps: float,
    qps: float,
    avg: float = 26.34,
    p95: float = 33.15,
    ignored: int = 0,
    reconnects: int = 0,
) -> str:
    """Build a minimal well-formed sysbench summary with the given metrics."""
    return (
        "SQL statistics:\n"
        f"    transactions:                        145780 ({tps} per sec.)\n"
        f"    queries:                             2915600 ({qps} per sec.)\n"
        f"    ignored errors:                      {ignored}      (0.00 per sec.)\n"
        f"    reconnects:                          {reconnects}      (0.00 per sec.)\n"
        "\n"
        "Latency (ms):\n"
        "         min:                                    1.15\n"
        f"         avg:                                   {avg}\n"
        "         max:                                  142.50\n"
        f"         95th percentile:                       {p95}\n"
    )


def _make_runner(run_outputs=None, fail=None):
    """Return (side_effect, calls) for a mocked subprocess.run.

    ``run_outputs`` are consumed in order by each ``run`` phase. ``fail`` is an
    optional callable ``(cmd, call_number) -> CompletedProcess | None`` used to
    force failures on specific invocations.
    """
    calls: list[tuple[list[str], dict]] = []
    outputs = list(run_outputs or [])

    def _side_effect(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if fail is not None:
            forced = fail(cmd, len(calls))
            if forced is not None:
                return forced
        phase = cmd[-1]
        if phase == "run":
            output = outputs.pop(0) if outputs else SAMPLE_SYSBENCH_OUTPUT
            return MagicMock(returncode=0, stdout=output, stderr="")
        if phase == "prepare":
            return MagicMock(returncode=0, stdout="Prepare completed", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return _side_effect, calls


def test_export_import():
    from src.knob_tuner.tools import run_sysbench_benchmark as fn
    from src.knob_tuner.tools.benchmark_tools import run_sysbench_measurement as fn2

    assert callable(fn)
    assert callable(fn2)


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
    assert details["ignored_errors"] == 0
    assert details["reconnects"] == 0


def test_parse_run_metrics():
    metrics = _parse_run_metrics(_output_with(123.5, 2470.0, avg=11.0, p95=22.0, ignored=1, reconnects=2))
    assert metrics["tps"] == 123.5
    assert metrics["qps"] == 2470.0
    assert metrics["latency_avg_ms"] == 11.0
    assert metrics["latency_p95_ms"] == 22.0
    assert metrics["ignored_errors"] == 1
    assert metrics["reconnects"] == 2


def test_measurement_deterministic_sequence_and_seed(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(
        tables=5,
        rows_per_table=1000,
        threads=8,
        warmup_seconds=5,
        measurement_seconds=30,
        repetitions=2,
        seed=123,
    )
    side_effect, calls = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        result = run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.status == "ok"
    assert result.threads == 8
    assert result.tables == 5
    assert result.rows_per_table == 1000
    assert result.duration == 30
    assert result.seed == 123
    assert result.repetitions == 2
    assert len(result.per_run_tps) == 2

    phases = [cmd[-1] for cmd, _ in calls]
    assert phases == [
        "cleanup",
        "prepare",
        "run",
        "cleanup",
        "prepare",
        "run",
        "cleanup",
        "prepare",
        "run",
    ]

    prepare_cmds = [cmd for cmd, _ in calls if cmd[-1] == "prepare"]
    run_cmds = [cmd for cmd, _ in calls if cmd[-1] == "run"]
    for cmd in prepare_cmds + run_cmds:
        assert "--rand-seed=123" in cmd

    warmup_run = run_cmds[0]
    measured_runs = run_cmds[1:]
    assert "--time=5" in warmup_run
    assert all("--time=30" in cmd for cmd in measured_runs)

    for cmd, _ in calls:
        assert "--report-interval" not in " ".join(cmd)
        assert "--tables=5" in cmd
        assert "--table-size=1000" in cmd
        assert "--threads=8" in cmd

    for _, kwargs in calls:
        assert "timeout" in kwargs
        assert kwargs["timeout"] > 0


def test_measurement_median_aggregation(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(
        tables=2,
        rows_per_table=100,
        threads=4,
        warmup_seconds=0,
        measurement_seconds=10,
        repetitions=3,
        seed=7,
    )
    outputs = [
        _output_with(100.0, 2000.0, avg=10.0, p95=5.0),
        _output_with(200.0, 4000.0, avg=20.0, p95=15.0),
        _output_with(300.0, 6000.0, avg=30.0, p95=25.0),
    ]
    side_effect, calls = _make_runner(run_outputs=outputs)

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        result = run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.status == "ok"
    assert result.per_run_tps == [100.0, 200.0, 300.0]
    assert result.tps == 200.0
    assert result.qps == 4000.0
    assert result.latency_avg_ms == 20.0
    assert result.latency_p95_ms == 15.0
    assert len(result.per_run_tps) == profile.repetitions


def test_measurement_malformed_output_is_error(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=10, repetitions=1)
    side_effect, _ = _make_runner(run_outputs=["not a sysbench summary"])

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        result = run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.status == "error"
    assert result.tps == 0.0
    assert "zero TPS" in result.error
    assert "repetition 1/1" in result.error


def test_measurement_ignored_errors_recorded_not_fatal(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=10, repetitions=2)
    outputs = [
        _output_with(100.0, 2000.0, ignored=2),
        _output_with(100.0, 2000.0, ignored=1),
    ]
    side_effect, _ = _make_runner(run_outputs=outputs)

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        result = run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.status == "ok"
    assert result.ignored_errors == 3
    assert result.error is None


def test_measurement_reconnects_is_error(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=10, repetitions=1)
    side_effect, _ = _make_runner(run_outputs=[_output_with(100.0, 2000.0, reconnects=4)])

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        result = run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.status == "error"
    assert result.reconnects == 4
    assert "reconnects=4" in result.error


def test_measurement_segfault_is_error_and_never_threads_one(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(threads=8, warmup_seconds=0, measurement_seconds=10, repetitions=1)

    def _fail(cmd, call_number):
        if cmd[-1] == "run":
            return MagicMock(returncode=-11, stdout="", stderr="Segmentation fault")
        return None

    side_effect, calls = _make_runner(fail=_fail)

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        result = run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.status == "error"
    assert "segmentation fault" in result.error
    assert result.threads == 8
    assert all("--threads=1" not in " ".join(cmd) for cmd, _ in calls)


def test_measurement_prepare_failure_is_error(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=10, repetitions=1)

    def _fail(cmd, call_number):
        if cmd[-1] == "prepare":
            return MagicMock(returncode=1, stdout="", stderr="FATAL: Disk full")
        return None

    side_effect, _ = _make_runner(fail=_fail)

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        result = run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.status == "error"
    assert "prepare failed" in result.error
    assert "Disk full" in result.error


def test_measurement_timeout_is_error(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=10, repetitions=1)

    def _timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=_timeout):
        result = run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.status == "error"
    assert "timed out" in result.error
    assert result.tps == 0.0


def test_measurement_unsupported_db_type_never_raises(tmp_path):
    cfg = DBConfig(
        host="localhost",
        port=1521,
        user="oracle",
        password="pwd",
        database="orcl",
        db_type="oracle",
        env="dev",
    )
    result = run_sysbench_measurement(cfg, SysbenchProfile(), workdir=str(tmp_path))
    assert result.status == "error"
    assert "Unsupported db_type 'oracle'" in result.error


def test_measurement_writes_combined_log(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=10, repetitions=1)
    side_effect, _ = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        result = run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert os.path.isfile(result.log_file)
    with open(result.log_file, encoding="utf-8") as f:
        content = f.read()
    assert "Prepare completed" in content
    assert "transactions:" in content


def test_legacy_wrapper_returns_legacy_and_new_keys(mock_db_config_pg, tmp_path):
    side_effect, calls = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        res = run_sysbench_benchmark(
            cfg=mock_db_config_pg,
            tables=50,
            table_size=100000,
            threads=32,
            duration=120,
            report_interval=60,
            workdir=str(tmp_path),
        )

    for key in (
        "status",
        "tps",
        "qps",
        "duration",
        "threads",
        "tables",
        "log_file",
        "error",
        "details",
        "ignored_errors",
        "reconnects",
        "latency_p95_ms",
        "per_run_tps",
        "seed",
        "repetitions",
    ):
        assert key in res

    assert res["status"] == "ok"
    assert res["duration"] == 120
    assert res["threads"] == 32
    assert res["tables"] == 50
    assert res["error"] is None
    assert res["seed"] == 42
    assert res["repetitions"] == 1
    assert len(res["per_run_tps"]) == 1
    assert res["details"]["latency_avg_ms"] == 26.34
    assert res["details"]["latency_95th_ms"] == 33.15
    assert res["details"]["ignored_errors"] == 0
    assert res["details"]["reconnects"] == 0
    assert os.path.isfile(res["log_file"])

    # report_interval is accepted but ignored (no --report-interval issued).
    assert all("--report-interval" not in " ".join(cmd) for cmd, _ in calls)
    assert all("--rand-seed=42" in cmd for cmd, _ in calls if cmd[-1] in ("prepare", "run"))


def test_legacy_wrapper_failure_returns_error_dict(mock_db_config_pg, tmp_path):
    def _fail(cmd, call_number):
        if cmd[-1] == "run":
            return MagicMock(returncode=127, stdout="", stderr="sysbench: command not found")
        return None

    side_effect, _ = _make_runner(fail=_fail)

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        res = run_sysbench_benchmark(cfg=mock_db_config_pg, workdir=str(tmp_path))

    assert res["status"] == "error"
    assert "exit code 127" in res["error"]
    assert res["tps"] == 0.0
    assert res["qps"] == 0.0
    assert res["details"]["latency_avg_ms"] == 0.0


def test_host_normalization_and_env_preserved(tmp_path):
    cfg_localhost = DBConfig(
        host="localhost",
        port=5432,
        user="postgres",
        password="test_secret_password",
        database="testdb",
        db_type="postgres",
        env="staging",
    )
    side_effect, calls = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        res = run_sysbench_benchmark(cfg=cfg_localhost, tables=10, workdir=str(tmp_path))

    assert res["status"] == "ok"
    assert res["tables"] == 10
    assert res["threads"] == 4

    cmd = calls[0][0]
    assert "--pgsql-host=127.0.0.1" in cmd
    assert "--tables=10" in cmd
    assert "--threads=4" in cmd

    call_env = calls[0][1].get("env", {})
    assert call_env.get("PGPASSWORD") == "test_secret_password"
    assert call_env.get("MYSQL_PWD") == "test_secret_password"


def test_legacy_wrapper_mysql_flags(tmp_path):
    cfg = DBConfig(
        host="127.0.0.1",
        port=3306,
        user="root",
        password="secretpassword",
        database="testdb",
        db_type="mysql",
        env="production",
    )
    side_effect, calls = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        res = run_sysbench_benchmark(cfg=cfg, tables=10, workdir=str(tmp_path))

    assert res["status"] == "ok"
    cmd = calls[0][0]
    assert "--db-driver=mysql" in cmd
    assert "--mysql-host=127.0.0.1" in cmd
    assert "--mysql-db=testdb" in cmd


def test_default_workdir(mock_db_config_pg, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    side_effect, _ = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        res = run_sysbench_benchmark(cfg=mock_db_config_pg, tables=10)

    assert res["status"] == "ok"
    assert res["log_file"] == os.path.join(
        "logs", "sysbench", os.path.basename(res["log_file"])
    )
    assert os.path.isfile(res["log_file"])


def test_measurement_emits_progress(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(
        tables=1,
        rows_per_table=10,
        threads=1,
        warmup_seconds=0,
        measurement_seconds=1,
        repetitions=2,
        seed=1,
    )
    messages: list[str] = []
    side_effect, _ = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        result = run_sysbench_measurement(
            mock_db_config_pg,
            profile,
            workdir=str(tmp_path),
            progress=messages.append,
        )

    assert result.status == "ok"
    assert any(m.startswith("Measurement:") for m in messages)
    assert any(m.startswith("rep 1/") for m in messages)
    assert any(m.startswith("rep 2/") for m in messages)
    assert any(m.startswith("Done:") for m in messages)


def test_measurement_silent_without_progress(mock_db_config_pg, tmp_path, capsys):
    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=1, repetitions=1)
    side_effect, _ = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert capsys.readouterr().out == ""
