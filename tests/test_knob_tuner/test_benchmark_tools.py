"""Unit tests for benchmark_tools module."""

import os
import subprocess
from unittest.mock import MagicMock, patch

from src.knob_tuner.contracts import SysbenchProfile
from src.knob_tuner.tools import run_sysbench_benchmark
from src.knob_tuner.tools.benchmark_tools import (
    _parse_pgbench_output,
    _parse_run_metrics,
    _parse_sysbench_summary,
    derive_screen_profile,
    run_pgbench_measurement,
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
    # Dataset is reset once per arm (cleanup+prepare), then warmup + measured
    # reps run back-to-back against the same warm dataset.
    assert phases == ["cleanup", "prepare", "run", "run", "run"]
    assert [cmd[-1] for cmd, _ in calls].count("prepare") == 1

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


def test_measurement_prepare_false_skips_cleanup_and_prepare(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(
        tables=3,
        rows_per_table=1000,
        threads=4,
        warmup_seconds=5,
        measurement_seconds=10,
        repetitions=2,
        seed=9,
    )
    messages: list[str] = []
    side_effect, calls = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        result = run_sysbench_measurement(
            mock_db_config_pg,
            profile,
            workdir=str(tmp_path),
            progress=messages.append,
            prepare=False,
        )

    assert result.status == "ok"
    assert result.prepare_seconds == 0.0
    phases = [cmd[-1] for cmd, _ in calls]
    assert "cleanup" not in phases
    assert "prepare" not in phases

    run_cmds = [cmd for cmd, _ in calls if cmd[-1] == "run"]
    assert len(run_cmds) == profile.repetitions + 1
    assert "--time=5" in run_cmds[0]
    assert len(run_cmds[1:]) == profile.repetitions
    assert all("--time=10" in cmd for cmd in run_cmds[1:])
    assert any("reusing prepared dataset (prepare=False)" in m for m in messages)


def test_measurement_rand_type_default_is_pareto(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=10, repetitions=1)
    side_effect, calls = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    for cmd, _ in calls:
        if cmd[-1] in ("prepare", "run"):
            assert "--rand-type=pareto" in cmd


def test_measurement_rand_type_override(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(
        warmup_seconds=0,
        measurement_seconds=10,
        repetitions=1,
        rand_type="uniform",
    )
    side_effect, calls = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    for cmd, _ in calls:
        if cmd[-1] in ("prepare", "run"):
            assert "--rand-type=uniform" in cmd


def test_measurement_prepare_seconds_nonnegative(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=10, repetitions=1)
    side_effect, _ = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect):
        result = run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.prepare_seconds >= 0.0


def test_derive_screen_profile_matches_target():
    base = SysbenchProfile(tables=10, rows_per_table=10000)
    profile, meta = derive_screen_profile(base, 1_000_000)

    assert meta["source"] == "target_rows"
    assert meta["capped"] is False
    assert meta["target_total_rows"] == 1_000_000
    assert meta["tables"] == 10
    assert meta["rows_per_table"] == 100_000
    assert profile.tables == 10
    assert profile.rows_per_table == 100_000
    assert meta["total_rows"] == meta["tables"] * meta["rows_per_table"]


def test_derive_screen_profile_caps_at_max_total_rows():
    base = SysbenchProfile(tables=4, rows_per_table=1000)
    profile, meta = derive_screen_profile(base, 50_000_000, max_total_rows=5_000_000)

    assert meta["source"] == "target_rows"
    assert meta["capped"] is True
    assert meta["target_total_rows"] == 50_000_000
    assert meta["tables"] == 4
    assert meta["rows_per_table"] == 5_000_000 // 4
    assert profile.tables == meta["tables"]
    assert profile.rows_per_table == meta["rows_per_table"]
    assert meta["total_rows"] == meta["tables"] * meta["rows_per_table"]


def test_derive_screen_profile_default_fallback():
    base = SysbenchProfile(tables=6, rows_per_table=500)
    for target in (None, 0):
        profile, meta = derive_screen_profile(base, target)

        assert meta["source"] == "default"
        assert meta["target_total_rows"] == 0
        assert meta["capped"] is False
        assert meta["tables"] == base.tables
        assert meta["rows_per_table"] == base.rows_per_table
        assert meta["total_rows"] == base.tables * base.rows_per_table
        assert profile is base


BENCH_PATCH_PREFIX = "src.knob_tuner.tools.benchmark_tools"


class _FakeClock:
    """Deterministic stand-in for ``time.monotonic``/``time.sleep``."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


def _run_settle(bt, cfg, responses):
    """Run ``_settle_after_load`` with a fake clock and scripted poll answers."""
    clock = _FakeClock()
    with patch.object(bt, "checkpoint_database", return_value=True) as ckpt, patch.object(
        bt, "run_safe_query", side_effect=responses
    ) as query, patch.object(
        bt.time, "monotonic", side_effect=clock.monotonic
    ), patch.object(bt.time, "sleep", side_effect=clock.sleep):
        bt._settle_after_load(cfg, lambda _m: None)
    return clock, ckpt, query


def test_settle_after_load_returns_after_minimum_settle(mock_db_config_pg):
    from src.knob_tuner.tools import benchmark_tools as bt

    emitted: list[str] = []
    clock = _FakeClock()
    with patch.object(bt, "checkpoint_database", return_value=True) as ckpt, patch.object(
        bt, "run_safe_query", return_value=[{"n": 0}]
    ) as query, patch.object(
        bt.time, "monotonic", side_effect=clock.monotonic
    ), patch.object(bt.time, "sleep", side_effect=clock.sleep):
        bt._settle_after_load(mock_db_config_pg, emitted.append)

    ckpt.assert_called_once_with(mock_db_config_pg)
    assert emitted == []
    # A single idle poll is not enough: the minimum settle must elapse too.
    assert clock.now >= bt._SETTLE_MIN_SECONDS
    assert query.call_count >= 2


def test_settle_after_load_requires_two_consecutive_quiescent_polls(
    mock_db_config_pg,
):
    from src.knob_tuner.tools import benchmark_tools as bt

    # An idle poll is interrupted by a worker, so the counter must reset and two
    # fresh consecutive idle polls are required.
    clock, _ckpt, query = _run_settle(
        bt, mock_db_config_pg, [[{"n": 0}], [{"n": 2}], [{"n": 0}], [{"n": 0}]]
    )

    assert query.call_count == 4
    assert clock.sleeps == 3


def test_settle_after_load_waits_while_autovacuum_active(mock_db_config_pg):
    from src.knob_tuner.tools import benchmark_tools as bt

    clock, _ckpt, query = _run_settle(
        bt, mock_db_config_pg, [[{"n": 2}], [{"n": 1}], [{"n": 0}], [{"n": 0}]]
    )

    assert query.call_count == 4
    assert clock.sleeps == 3


def test_settle_after_load_times_out_and_warns(mock_db_config_pg):
    from src.knob_tuner.tools import benchmark_tools as bt

    emitted: list[str] = []
    clock = _FakeClock()
    with patch.object(bt, "checkpoint_database", return_value=True), patch.object(
        bt, "run_safe_query", return_value=[{"n": 1}]
    ), patch.object(
        bt.time, "monotonic", side_effect=clock.monotonic
    ), patch.object(bt.time, "sleep", side_effect=clock.sleep):
        bt._settle_after_load(mock_db_config_pg, emitted.append)

    assert clock.now >= bt._SETTLE_TIMEOUT_SECONDS
    assert any("timeout" in message for message in emitted)


def test_settle_after_load_never_raises_when_queries_fail(mock_db_config_pg):
    from src.knob_tuner.tools import benchmark_tools as bt

    with patch.object(bt, "checkpoint_database", side_effect=RuntimeError("boom")), patch.object(
        bt, "run_safe_query", side_effect=RuntimeError("boom")
    ):
        bt._settle_after_load(mock_db_config_pg, lambda _m: None)


def test_settle_after_load_skips_non_postgres(mock_db_config_mysql):
    from src.knob_tuner.tools import benchmark_tools as bt

    with patch.object(bt, "checkpoint_database") as ckpt, patch.object(
        bt, "run_safe_query"
    ) as query:
        bt._settle_after_load(mock_db_config_mysql, lambda _m: None)

    ckpt.assert_not_called()
    query.assert_not_called()


def test_measurement_settles_database_before_measuring(mock_db_config_pg, tmp_path):
    from src.knob_tuner.tools import benchmark_tools as bt

    profile = SysbenchProfile(
        tables=1,
        rows_per_table=10,
        threads=1,
        warmup_seconds=1,
        measurement_seconds=1,
        repetitions=1,
    )
    side_effect, _calls = _make_runner()

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect), patch.object(
        bt, "_settle_after_load"
    ) as settle:
        result = run_sysbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.status == "ok"
    settle.assert_called_once()


SAMPLE_PGBENCH_OUTPUT = """
pgbench (18.6)
number of clients: 2
number of threads: 2
duration: 30 s
number of transactions actually processed: 60
number of failed transactions: 0 (0.000%)
latency average = 1000.123 ms
initial connection time = 45.678 ms
tps = 1.998765 (without initial connection time)
"""


def _pgbench_output(tps: float, latency: float = 1000.0, failed: int = 0) -> str:
    """Build a minimal well-formed pgbench summary with the given metrics."""
    return (
        "pgbench (18.6)\n"
        "number of transactions actually processed: 60\n"
        f"number of failed transactions: {failed} (0.000%)\n"
        f"latency average = {latency} ms\n"
        f"tps = {tps} (without initial connection time)\n"
    )


def _make_pgbench_runner(run_outputs=None, fail=None):
    """Return (side_effect, calls) mimicking sysbench cleanup/prepare + pgbench runs."""
    calls: list[tuple[list[str], dict]] = []
    outputs = list(run_outputs or [])

    def _side_effect(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if fail is not None:
            forced = fail(cmd, len(calls))
            if forced is not None:
                return forced
        joined = " ".join(cmd)
        if "pgbench" in joined and "-T" in cmd:
            output = outputs.pop(0) if outputs else SAMPLE_PGBENCH_OUTPUT
            return MagicMock(returncode=0, stdout=output, stderr="")
        if cmd[-1] in ("cleanup", "prepare"):
            return MagicMock(returncode=0, stdout="done", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return _side_effect, calls


def test_parse_pgbench_output():
    metrics = _parse_pgbench_output(SAMPLE_PGBENCH_OUTPUT)
    assert metrics["tps"] == 1.998765
    assert metrics["latency_avg_ms"] == 1000.123
    assert metrics["failed_transactions"] == 0


def test_parse_pgbench_output_malformed_is_zero():
    metrics = _parse_pgbench_output("pgbench: could not connect")
    assert metrics["tps"] == 0.0
    assert metrics["latency_avg_ms"] == 0.0
    assert metrics["failed_transactions"] == 0


def test_run_pgbench_measurement_median_aggregation(mock_db_config_pg, tmp_path):
    from src.knob_tuner.tools import benchmark_tools as bt

    profile = SysbenchProfile(
        tables=1,
        rows_per_table=100,
        threads=2,
        warmup_seconds=0,
        measurement_seconds=10,
        repetitions=3,
        seed=11,
    )
    outputs = [
        _pgbench_output(1.0, latency=100.0),
        _pgbench_output(2.0, latency=200.0),
        _pgbench_output(3.0, latency=300.0),
    ]
    side_effect, calls = _make_pgbench_runner(run_outputs=outputs)

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect), patch.object(
        bt, "_settle_after_load"
    ) as settle:
        result = run_pgbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.status == "ok"
    assert result.per_run_tps == [1.0, 2.0, 3.0]
    assert result.tps == 2.0
    assert result.latency_avg_ms == 200.0
    assert result.tables == 1
    assert result.rows_per_table == 100
    assert result.repetitions == 3
    assert result.seed == 11
    settle.assert_called_once()

    phases = [cmd[-1] for cmd, _ in calls]
    assert "cleanup" in phases
    assert "prepare" in phases
    pgbench_cmds = [cmd for cmd, _ in calls if "pgbench" in " ".join(cmd) and "-T" in cmd]
    assert len(pgbench_cmds) == 3
    for cmd in pgbench_cmds:
        assert "-f" in cmd
        assert mock_db_config_pg.database in cmd


def test_run_pgbench_measurement_prepare_failure_is_error(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=10, repetitions=1)

    def _fail(cmd, call_number):
        if cmd[-1] == "prepare":
            return MagicMock(returncode=1, stdout="", stderr="FATAL: Disk full")
        return None

    side_effect, _ = _make_pgbench_runner(fail=_fail)

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect), patch(
        "src.knob_tuner.tools.benchmark_tools._settle_after_load"
    ):
        result = run_pgbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.status == "error"
    assert "prepare failed" in result.error


def test_run_pgbench_measurement_rejects_non_postgres(tmp_path):
    cfg = DBConfig(
        host="127.0.0.1",
        port=3306,
        user="root",
        password="pwd",
        database="testdb",
        db_type="mysql",
        env="staging",
    )
    result = run_pgbench_measurement(cfg, SysbenchProfile(), workdir=str(tmp_path))
    assert result.status == "error"
    assert "PostgreSQL" in result.error


def test_run_pgbench_measurement_zero_tps_is_error(mock_db_config_pg, tmp_path):
    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=10, repetitions=1)
    side_effect, _ = _make_pgbench_runner(run_outputs=["pgbench: could not connect"])

    with patch(DB_CONN_PATCH), patch(RUN_PATCH, side_effect=side_effect), patch(
        "src.knob_tuner.tools.benchmark_tools._settle_after_load"
    ):
        result = run_pgbench_measurement(mock_db_config_pg, profile, workdir=str(tmp_path))

    assert result.status == "error"
    assert "zero TPS" in result.error


def test_benchmark_mutates_dataset_sysbench():
    from src.knob_tuner.tools.benchmark_tools import benchmark_mutates_dataset

    assert (
        benchmark_mutates_dataset(
            "sysbench", SysbenchProfile(profile_type="oltp_read_write")
        )
        is True
    )
    assert (
        benchmark_mutates_dataset(
            "sysbench", SysbenchProfile(profile_type="oltp_read_only")
        )
        is False
    )
    assert (
        benchmark_mutates_dataset(
            "sysbench", SysbenchProfile(profile_type="point_select")
        )
        is False
    )
    # Unknown tests are treated as mutating (conservative).
    assert (
        benchmark_mutates_dataset("sysbench", SysbenchProfile(profile_type="weird"))
        is True
    )


def test_benchmark_mutates_dataset_pgbench_bundled_script_is_read_only():
    from src.knob_tuner.tools.benchmark_tools import benchmark_mutates_dataset

    assert benchmark_mutates_dataset("pgbench", SysbenchProfile()) is False


def test_pgbench_script_mutation_detection(tmp_path):
    from src.knob_tuner.tools.benchmark_tools import _pgbench_script_mutates

    read_only = tmp_path / "ro.pgb"
    read_only.write_text(
        "-- a comment mentioning update should be ignored\nSELECT count(*) FROM t;\n",
        encoding="utf-8",
    )
    mutating = tmp_path / "rw.pgb"
    mutating.write_text("UPDATE t SET x = 1;\n", encoding="utf-8")

    assert _pgbench_script_mutates(str(read_only)) is False
    assert _pgbench_script_mutates(str(mutating)) is True
    # Unreadable script -> assume mutating (conservative).
    assert _pgbench_script_mutates(str(tmp_path / "missing.pgb")) is True
