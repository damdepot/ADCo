"""Unit tests for the deterministic validation orchestrator."""

from unittest.mock import patch

import pytest

from src.knob_tuner.contracts import (
    ApplyMode,
    KnobPlan,
    KnobScope,
    KnobSpec,
    ResourceBudget,
    SysbenchMeasurement,
    SysbenchProfile,
)
from src.knob_tuner.tools.db_connector import DBConfig
from src.knob_tuner.tools.validation import (
    SnapshotRegistry,
    _collect_pg_write_stats,
    _enable_wal_io_timing,
    _pooled_baseline,
    _snapshot_delta,
    validate_plan,
)

_VALIDATION = "src.knob_tuner.tools.validation"


class _FakeCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str) -> None:
        self.statements.append(sql)

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self) -> None:
        self.autocommit = False
        self.cursor_obj = _FakeCursor()
        self.committed = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def _wal_state(
    wal_records: int,
    wal_bytes: int,
    xact_commit: int,
) -> dict:
    return {
        "wal": {
            "wal_records": wal_records,
            "wal_fpi": 1,
            "wal_bytes": wal_bytes,
            "wal_write_time": 10,
            "wal_sync_time": 5,
        },
        "bgwriter": {
            "checkpoints_timed": 1,
            "checkpoints_req": 0,
            "buffers_checkpoint": 100,
            "buffers_clean": 10,
            "buffers_backend": 20,
        },
        "database": {
            "blk_read_time": 1,
            "blk_write_time": 2,
            "xact_commit": xact_commit,
            "xact_rollback": 0,
        },
    }


def _stats_responder(states: list[dict]):
    calls = {"count": 0}

    def responder(cfg, sql, params=None):
        state = states[calls["count"] // 4]
        calls["count"] += 1
        if "pg_stat_wal" in sql:
            return [state["wal"]]
        if "pg_stat_bgwriter" in sql:
            return [state["bgwriter"]]
        if "pg_stat_database" in sql:
            return [state["database"]]
        return []

    return responder


@pytest.fixture
def budget() -> ResourceBudget:
    return ResourceBudget(cpu_cores=2, memory_gb=2)


@pytest.fixture
def profile() -> SysbenchProfile:
    return SysbenchProfile(
        tables=1,
        rows_per_table=10,
        threads=1,
        warmup_seconds=0,
        measurement_seconds=1,
        repetitions=2,
        seed=42,
        throughput_threshold_pct=0.0,
        latency_threshold_pct=5.0,
    )


@pytest.fixture
def plan() -> KnobPlan:
    return KnobPlan(
        knobs=[
            KnobSpec(
                name="work_mem",
                value="64MB",
                scope=KnobScope.USER,
                restart_required=False,
            ),
            KnobSpec(
                name="shared_buffers",
                value="1GB",
                scope=KnobScope.POSTMASTER,
                restart_required=True,
            ),
        ]
    )


@pytest.fixture
def staging_cfg() -> DBConfig:
    return DBConfig(
        host="127.0.0.1",
        port=55000,
        user="postgres",
        password="postgres",
        database="testdb",
        db_type="postgres",
        env="staging",
    )


def _measurement(
    per_run_tps: list[float],
    ignored_errors: int = 0,
    *,
    status: str = "ok",
    error: str | None = None,
) -> SysbenchMeasurement:
    return SysbenchMeasurement(
        status=status,
        tps=sum(per_run_tps) / len(per_run_tps) if per_run_tps else 0.0,
        qps=sum(per_run_tps) / len(per_run_tps) if per_run_tps else 0.0,
        latency_avg_ms=1.0,
        latency_p95_ms=2.0,
        ignored_errors=ignored_errors,
        reconnects=0,
        threads=1,
        tables=1,
        rows_per_table=10,
        duration=1,
        seed=42,
        repetitions=len(per_run_tps),
        per_run_tps=list(per_run_tps),
        error=error,
    )


def _verify(all_verified: bool = True, mismatch: bool = False) -> dict:
    knobs = [
        {
            "knob": "work_mem",
            "expected_value": "64MB",
            "actual_value": "64MB" if not mismatch else "4MB",
            "unit": "",
            "pending_restart": False,
            "status": "MISMATCH" if mismatch else "VERIFIED",
        },
        {
            "knob": "shared_buffers",
            "expected_value": "1GB",
            "actual_value": "1GB",
            "unit": "",
            "pending_restart": False,
            "status": "VERIFIED",
        },
    ]
    return {"status": "ok", "all_verified": all_verified, "knobs": knobs, "error": None}


def _apply_results(fail_first: bool = False) -> list[dict]:
    return [
        {
            "knob": "work_mem",
            "value": "64MB",
            "status": "failed" if fail_first else "applied",
            "sql": "ALTER SYSTEM SET work_mem = '64MB';",
            "error": "syntax error" if fail_first else None,
        },
        {
            "knob": "shared_buffers",
            "value": "1GB",
            "status": "applied",
            "sql": "ALTER SYSTEM SET shared_buffers = '1GB';",
            "error": None,
        },
    ]


def _call_kwargs(budget, profile, plan):
    return dict(
        run_id="run-1",
        run_dir="/tmp/run-1",
        plan=plan,
        budget=budget,
        profile=profile,
        db_type="postgres",
        db_version="17",
        database="testdb",
        workdir="/tmp/logs",
    )


def test_dry_run_skips_everything(budget, profile, plan):
    with (
        patch(f"{_VALIDATION}.start_staging_db") as m_start,
        patch(f"{_VALIDATION}.run_sysbench_measurement") as m_bench,
        patch(f"{_VALIDATION}.apply_knobs") as m_apply,
        patch(f"{_VALIDATION}.restart_docker_db") as m_restart,
        patch(f"{_VALIDATION}.stop_staging_db") as m_stop,
        patch(f"{_VALIDATION}.write_artifact") as m_write,
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
            dry_run=True,
        )

    m_start.assert_not_called()
    m_bench.assert_not_called()
    m_apply.assert_not_called()
    m_restart.assert_not_called()
    m_stop.assert_not_called()
    m_write.assert_not_called()

    assert result["status"] == "INCONCLUSIVE"
    assert result["attestation"] is None
    assert result["paired"] is None
    assert result["staging_db_config"] is None
    assert result["artifacts"] == {}
    assert any("dry-run" in reason for reason in result["reasons"])


def test_happy_path_pass(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])
    reversal = _measurement([100.0, 100.0])
    before = {"work_mem": "4096", "shared_buffers": "16384"}
    after = {"work_mem": "65536", "shared_buffers": "1048576"}

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ) as m_start,
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned, reversal],
        ) as m_bench,
        patch(
            f"{_VALIDATION}.apply_knobs", return_value=_apply_results()
        ) as m_apply,
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()) as _m_verify,
        patch(f"{_VALIDATION}.restart_docker_db") as m_restart,
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")) as m_stop,
        patch(
            f"{_VALIDATION}.snapshot_settings",
            side_effect=[before, after],
        ) as m_snapshot,
        patch(
            f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"
        ) as m_write,
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    assert result["status"] == "PASS"
    assert result["paired"]["status"] == "PASS"
    assert result["paired"]["median_tps_tuned"] > result["paired"]["median_tps_baseline"]

    attestation = result["attestation"]
    assert attestation is not None
    assert attestation["status"] == "PASS"
    assert attestation["plan_hash"] == plan.plan_hash()
    assert attestation["profile_hash"] == profile.profile_hash()
    assert attestation["seed"] == profile.seed
    assert attestation["attempts"] == 1
    assert attestation["resource_budget"] == budget.model_dump()
    assert attestation["settings_before"] == before
    assert attestation["settings_after"] == after
    assert len(attestation["verified_knobs"]) == 2
    assert attestation["database_identity"].startswith("postgres://127.0.0.1:55000/")

    m_start.assert_called_once()
    assert m_apply.call_count == 2  # forward apply + baseline revert
    m_restart.assert_not_called()
    m_stop.assert_called_once_with("stg-container")
    assert m_snapshot.call_count == 2

    # The baseline is measured twice (A1 before, A2 after) so the candidate is
    # bracketed, and the pooled per-run TPS is used for the verdict.
    assert m_bench.call_count == 3
    assert result["paired"]["baseline"]["per_run_tps"] == [100.0, 100.0, 100.0, 100.0]
    assert result["paired"]["median_tps_baseline"] == 100.0

    written_names = [call.args[1] for call in m_write.call_args_list]
    assert "sysbench-baseline" in written_names
    assert "sysbench-tuned" in written_names
    assert "sysbench-baseline-reversal" in written_names
    assert "settings-after" in written_names
    assert "validation-attempt-1" in written_names

    # Probe instrumentation: phase timings are recorded for the economics gate.
    timings = result["artifacts"]["timings"]
    assert "provision_seconds" in timings
    assert "baseline_seconds" in timings
    assert "measurement_seconds" in timings
    assert "baseline_reversal_seconds" in timings
    assert "revert_seconds" in timings

    baseline_call, tuned_call, reversal_call = m_bench.call_args_list
    assert baseline_call.kwargs["prepare"] is True
    assert tuned_call.kwargs["prepare"] is True
    assert reversal_call.kwargs["prepare"] is True
    assert timings["prepare_seconds"] == baseline.prepare_seconds

    # The reversal is machine-readable, not just a reason string.
    assert result["baseline_reversal"]["measured"] is True
    assert result["baseline_reversal"]["reason"] == ""
    assert "baseline-reversal-status" in written_names


def test_pgbench_dispatch_selects_pgbench_runner(budget, profile, plan, staging_cfg):
    baseline = _measurement([1.0, 1.0])
    tuned = _measurement([8.0, 8.0])
    reversal = _measurement([1.0, 1.0])
    before = {"work_mem": "4096", "shared_buffers": "16384"}

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_pgbench_measurement",
            side_effect=[baseline, tuned, reversal],
        ) as m_pg,
        patch(f"{_VALIDATION}.run_sysbench_measurement") as m_sys,
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=[before, before]),
        patch(
            f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"
        ) as m_write,
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
            benchmark_kind="pgbench",
        )

    assert m_pg.call_count == 3
    m_sys.assert_not_called()
    assert result["status"] == "PASS"
    written_names = [call.args[1] for call in m_write.call_args_list]
    assert "pgbench-baseline" in written_names
    assert "pgbench-tuned" in written_names
    assert "pgbench-baseline-reversal" in written_names


def test_default_dispatch_selects_sysbench_runner(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])
    reversal = _measurement([100.0, 100.0])
    before = {"work_mem": "4096", "shared_buffers": "16384"}

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned, reversal],
        ) as m_sys,
        patch(f"{_VALIDATION}.run_pgbench_measurement") as m_pg,
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=[before, before]),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    assert m_sys.call_count == 3
    m_pg.assert_not_called()


@pytest.mark.parametrize("reuse_dataset", [True, False])
def test_every_arm_prepares_dataset_symmetrically(
    budget, profile, plan, staging_cfg, reuse_dataset
):
    baseline = _measurement([100.0, 100.0])
    baseline.prepare_seconds = 2.5
    tuned = _measurement([112.0, 110.0])
    reversal = _measurement([100.0, 100.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned, reversal],
        ) as m_bench,
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
            reuse_dataset=reuse_dataset,
        )

    assert m_bench.call_count == 3
    assert all(call.kwargs["prepare"] is True for call in m_bench.call_args_list)
    assert result["artifacts"]["timings"]["prepare_seconds"] == baseline.prepare_seconds


def test_read_only_benchmark_prepares_once_and_reuses(
    budget, profile, plan, staging_cfg
):
    baseline = _measurement([1.0, 1.0])
    tuned = _measurement([8.0, 8.0])
    reversal = _measurement([1.0, 1.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_pgbench_measurement",
            side_effect=[baseline, tuned, reversal],
        ) as m_pg,
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
            benchmark_kind="pgbench",
        )

    # The pgbench sort/hash script is read-only, so only the first arm loads the
    # dataset; re-preparing a multi-million-row dataset per arm is pure waste.
    assert [call.kwargs["prepare"] for call in m_pg.call_args_list] == [
        True,
        False,
        False,
    ]


def test_pooled_baseline_refuses_non_ok_arm():
    ok = _measurement([100.0, 100.0])
    failed = _measurement([50.0, 60.0], status="error", error="boom")

    pooled = _pooled_baseline(ok, failed)
    assert pooled.status == "error"
    assert "cannot pool non-ok baseline arms" in pooled.error
    assert "A2" in pooled.error

    pooled_first_bad = _pooled_baseline(failed, ok)
    assert pooled_first_bad.status == "error"
    assert "A1" in pooled_first_bad.error

    healthy = _pooled_baseline(ok, _measurement([104.0, 106.0]))
    assert healthy.status == "ok"
    assert healthy.error is None
    assert healthy.per_run_tps == [100.0, 100.0, 104.0, 106.0]


def test_failed_baseline_is_not_resurrected_by_pooling(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0], status="error", error="partial failure")
    tuned = _measurement([130.0, 130.0])
    reversal = _measurement([100.0, 100.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned, reversal],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    assert result["paired"]["baseline"]["status"] == "error"
    assert result["paired"]["status"] == "INCONCLUSIVE"
    assert result["status"] == "INCONCLUSIVE"


def test_inconclusive_when_missing_reps(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0])
    tuned = _measurement([110.0, 111.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=[{}, {}]),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    assert result["status"] == "INCONCLUSIVE"
    assert result["paired"]["status"] == "INCONCLUSIVE"
    assert result["attestation"]["status"] == "INCONCLUSIVE"
    assert any("repetitions" in reason for reason in result["reasons"])


def test_fail_when_knob_application_fails(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results(fail_first=True)),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=[{}, {}]),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    assert result["paired"]["status"] == "PASS"
    assert result["status"] == "FAIL"
    assert result["attestation"]["status"] == "FAIL"
    assert any("application failed" in reason for reason in result["reasons"])


def test_fail_when_verification_mismatch(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(
            f"{_VALIDATION}.verify_active_knobs",
            return_value=_verify(all_verified=False, mismatch=True),
        ),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=[{}, {}]),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    assert result["paired"]["status"] == "PASS"
    assert result["status"] == "FAIL"
    assert any("verification" in reason and "MISMATCH" in reason for reason in result["reasons"])


def test_environment_error_at_provisioning(budget, profile, plan):
    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            side_effect=RuntimeError("docker unavailable"),
        ),
        patch(f"{_VALIDATION}.apply_knobs") as m_apply,
        patch(f"{_VALIDATION}.run_sysbench_measurement") as m_bench,
        patch(f"{_VALIDATION}.restart_docker_db") as m_restart,
        patch(f"{_VALIDATION}.stop_staging_db") as m_stop,
        patch(f"{_VALIDATION}.write_artifact") as m_write,
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    assert result["status"] == "FAIL"
    assert result["attestation"] is None
    assert result["staging_db_config"] is None
    assert any("environment error" in reason for reason in result["reasons"])
    m_apply.assert_not_called()
    m_bench.assert_not_called()
    m_restart.assert_not_called()
    m_stop.assert_not_called()
    m_write.assert_not_called()


def test_persist_static_restarts_and_refreshes_port(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(
            f"{_VALIDATION}.restart_docker_db", return_value=(True, "restarted")
        ) as m_restart,
        patch(
            f"{_VALIDATION}.get_container_host_port", return_value=55001
        ) as m_port,
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=[{}, {}]),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.MANUAL,
        )

    assert result["status"] == "PASS"
    m_restart.assert_called_once_with("stg-container", db_type="postgres")
    m_port.assert_called_once()
    assert result["staging_db_config"].port == 55001


def test_restart_failure_returns_fail(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(
            f"{_VALIDATION}.restart_docker_db", return_value=(False, "boom")
        ) as m_restart,
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=[{}, {}]),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")) as m_stop,
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.MANUAL,
        )

    assert result["status"] == "FAIL"
    assert any("restart failed" in reason for reason in result["reasons"])
    m_restart.assert_called_once()
    m_stop.assert_called_once_with("stg-container")


def test_validate_plan_emits_progress(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])
    messages: list[str] = []

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=[{}, {}]),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
            progress=messages.append,
        )

    assert result["status"] == "PASS"
    joined = "\n".join(messages)
    assert "provisioning staging" in joined
    assert "applying" in joined
    assert "verifying" in joined
    assert "verdict:" in joined


def test_collect_pg_write_stats_tolerates_missing_columns(staging_cfg):
    def responder(cfg, sql, params=None):
        if "pg_stat_wal" in sql:
            return [{"wal_records": 5, "wal_fpi": 0, "wal_bytes": 100}]
        if "pg_stat_bgwriter" in sql:
            return [{"buffers_clean": 7}]
        if "pg_stat_database" in sql:
            return [{"xact_commit": 3, "xact_rollback": 0, "blk_read_time": 1.0}]
        return [{"num_timed": 2, "num_requested": 1, "buffers_written": 9}]

    with patch(f"{_VALIDATION}.run_safe_query", side_effect=responder):
        stats = _collect_pg_write_stats(staging_cfg)

    assert stats["wal_records"] == 5.0
    assert stats["buffers_clean"] == 7.0
    assert stats["xact_commit"] == 3.0
    assert stats["checkpoints_timed"] == 2.0
    assert stats["checkpoints_req"] == 1.0
    assert stats["buffers_checkpoint"] == 9.0
    assert "wal_sync_time" not in stats
    assert "buffers_backend" not in stats


def test_snapshot_delta_computes_differences_and_guards_zero():
    before = {"wal_bytes": 100.0, "xact_commit": 10.0, "wal_records": 5.0}
    after = {"wal_bytes": 250.0, "xact_commit": 20.0, "wal_records": 12.0}
    delta = _snapshot_delta(before, after)

    assert delta["wal_bytes"] == 150.0
    assert delta["xact_commit"] == 10.0
    assert delta["wal_records"] == 7.0
    assert delta["wal_bytes_per_commit"] == 15.0

    missing_key = _snapshot_delta({}, {"wal_records": 4.0})
    assert missing_key["wal_records"] == 4.0

    no_commits = _snapshot_delta({"wal_bytes": 10.0}, {"wal_bytes": 30.0})
    assert no_commits["wal_bytes_per_commit"] == 0.0


def test_collect_pg_write_stats_never_raises(staging_cfg):
    with patch(
        f"{_VALIDATION}.run_safe_query", side_effect=RuntimeError("no connection")
    ):
        assert _collect_pg_write_stats(staging_cfg) == {}


def test_enable_wal_io_timing_uses_alter_system(staging_cfg):
    fake = _FakeConn()
    with patch(f"{_VALIDATION}.get_connection", return_value=fake):
        assert _enable_wal_io_timing(staging_cfg) is True

    assert "ALTER SYSTEM SET track_wal_io_timing = on;" in fake.cursor_obj.statements
    assert "SELECT pg_reload_conf();" in fake.cursor_obj.statements
    assert fake.autocommit is True
    assert fake.closed is True


def test_enable_wal_io_timing_failure_returns_false(staging_cfg):
    with patch(
        f"{_VALIDATION}.get_connection", side_effect=RuntimeError("permission denied")
    ):
        assert _enable_wal_io_timing(staging_cfg) is False


def test_validate_plan_records_wal_evidence(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])
    states = [
        _wal_state(wal_records=100, wal_bytes=1000, xact_commit=50),
        _wal_state(wal_records=200, wal_bytes=2000, xact_commit=100),
        _wal_state(wal_records=350, wal_bytes=3500, xact_commit=190),
        _wal_state(wal_records=500, wal_bytes=5000, xact_commit=280),
    ]
    fake_conn = _FakeConn()

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=[{}, {}]),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
        patch(
            f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"
        ) as m_write,
        patch(f"{_VALIDATION}.get_connection", return_value=fake_conn),
        patch(
            f"{_VALIDATION}.run_safe_query", side_effect=_stats_responder(states)
        ),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    assert result["status"] == "PASS"
    assert "ALTER SYSTEM SET track_wal_io_timing = on;" in fake_conn.cursor_obj.statements

    evidence = result["wal_evidence"]
    assert evidence["snapshots"]["before"]["wal_records"] == 100.0
    assert evidence["snapshots"]["baseline_after"]["wal_records"] == 200.0
    assert evidence["snapshots"]["tuned_before"]["wal_records"] == 350.0
    assert evidence["snapshots"]["after"]["wal_records"] == 500.0

    assert evidence["baseline_delta"]["wal_records"] == 100.0
    assert evidence["tuned_delta"]["wal_records"] == 150.0
    assert evidence["delta"]["wal_records"] == 50.0
    assert evidence["delta"]["wal_bytes"] == 500.0
    assert evidence["delta"]["xact_commit"] == 40.0
    assert evidence["delta"]["wal_bytes_per_commit"] == 12.5

    assert result["artifacts"]["wal_evidence"] == "/tmp/artifact.json"
    written = {
        call.args[1]: call.args[2] for call in m_write.call_args_list
    }
    assert written["wal-evidence"]["delta"]["wal_records"] == 50.0


def test_validate_plan_survives_evidence_failures(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=[{}, {}]),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(
            f"{_VALIDATION}.get_connection", side_effect=RuntimeError("denied")
        ),
        patch(
            f"{_VALIDATION}.run_safe_query", side_effect=RuntimeError("denied")
        ),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    assert result["status"] == "PASS"
    assert result["wal_evidence"] is not None
    assert result["wal_evidence"]["delta"]["wal_bytes_per_commit"] == 0.0
    assert result["wal_evidence"]["snapshots"]["before"] == {}


_BEFORE = {"work_mem": "4096", "shared_buffers": "16384"}
_AFTER = {"work_mem": "65536", "shared_buffers": "1048576"}


def _settings_seq(before=None):
    return [before if before is not None else _BEFORE, _AFTER]


def test_reversal_measures_baseline_twice_and_pools(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])
    reversal = _measurement([104.0, 106.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned, reversal],
        ) as m_bench,
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()) as m_apply,
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json") as m_write,
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    # A1, candidate B and the restored baseline A2 were all measured.
    assert m_bench.call_count == 3
    assert m_apply.call_count == 2  # forward apply + revert

    # The verdict compares against the pooled A1+A2 per-run samples.
    assert result["paired"]["baseline"]["per_run_tps"] == [100.0, 100.0, 104.0, 106.0]
    assert result["paired"]["median_tps_baseline"] == 102.0

    written_names = [call.args[1] for call in m_write.call_args_list]
    assert "sysbench-baseline-reversal" in written_names
    assert "baseline_reversal_seconds" in result["artifacts"]["timings"]

    # The revert re-applies the values captured before the plan.
    revert_call = m_apply.call_args_list[1]
    reverted = {entry["name"]: entry["value"] for entry in revert_call.args[0]}
    assert reverted == _BEFORE


def test_reversal_cancels_cold_warm_bias(budget, profile, plan, staging_cfg):
    cold = _measurement([100.0, 100.0])
    candidate = _measurement([110.0, 110.0])
    warm = _measurement([120.0, 120.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[cold, candidate, warm],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    # A linear cold->warm drift puts the candidate exactly at the pooled median,
    # so the spurious gain (10% against the cold A1 alone) cancels out.
    assert result["paired"]["median_tps_baseline"] == 110.0
    assert result["paired"]["median_tps_tuned"] == 110.0
    assert abs(result["paired"]["delta_pct"]) < 1e-9


def test_reversal_genuine_win_still_passes(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([130.0, 130.0])
    reversal = _measurement([100.0, 100.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned, reversal],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    assert result["status"] == "PASS"
    assert result["paired"]["status"] == "PASS"
    assert result["paired"]["median_tps_baseline"] == 100.0
    assert result["paired"]["delta_pct"] == 30.0


def test_reversal_falls_back_when_second_baseline_fails(
    budget, profile, plan, staging_cfg
):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])
    failed_reversal = SysbenchMeasurement(status="error", error="sysbench crashed")

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned, failed_reversal],
        ) as m_bench,
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json") as m_write,
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    # A failed reversal must never fail the run and must fall back to A1.
    assert result["status"] == "PASS"
    assert m_bench.call_count == 3
    assert result["paired"]["baseline"]["per_run_tps"] == [100.0, 100.0]
    assert any("reversal not measured" in reason for reason in result["reasons"])
    written_names = [call.args[1] for call in m_write.call_args_list]
    assert "sysbench-baseline-reversal" not in written_names


def test_reversal_falls_back_when_revert_fails(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])
    revert_failure = [
        {
            "knob": "work_mem",
            "value": "4096",
            "status": "failed",
            "sql": "",
            "error": "permission denied",
        }
    ]

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned],
        ) as m_bench,
        patch(
            f"{_VALIDATION}.apply_knobs",
            side_effect=[_apply_results(), revert_failure],
        ),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    assert result["status"] == "PASS"
    assert m_bench.call_count == 2  # A2 never ran
    assert result["paired"]["baseline"]["per_run_tps"] == [100.0, 100.0]
    assert any("revert failed" in reason for reason in result["reasons"])


def test_reversal_restarts_staging_after_revert(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])
    reversal = _measurement([100.0, 100.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned, reversal],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(
            f"{_VALIDATION}.restart_docker_db", return_value=(True, "restarted")
        ) as m_restart,
        patch(f"{_VALIDATION}.get_container_host_port", return_value=55001) as m_port,
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.MANUAL,
        )

    assert result["status"] == "PASS"
    assert m_restart.call_count == 2  # forward apply + revert
    assert m_port.call_count == 2
    assert "revert_restart_seconds" in result["artifacts"]["timings"]


def test_reversal_skipped_without_settings_snapshot(
    budget, profile, plan, staging_cfg
):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned],
        ) as m_bench,
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=[{}, {}]),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    assert result["status"] == "PASS"
    assert m_bench.call_count == 2
    assert result["paired"]["baseline"]["per_run_tps"] == [100.0, 100.0]
    assert any(
        "no pre-plan settings snapshot" in reason for reason in result["reasons"]
    )
    assert result["baseline_reversal"] == {
        "measured": False,
        "reason": "no pre-plan settings snapshot",
    }


def test_reversal_failure_is_visible_in_result(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])
    failed_reversal = SysbenchMeasurement(status="error", error="sysbench crashed")

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned, failed_reversal],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(
            f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"
        ) as m_write,
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    # The run still passes on the single A1 baseline...
    assert result["status"] == "PASS"
    assert result["paired"]["baseline"]["per_run_tps"] == [100.0, 100.0]
    # ...but the skipped reversal is explicit and machine-readable.
    assert result["baseline_reversal"]["measured"] is False
    assert "status is 'error'" in result["baseline_reversal"]["reason"]
    assert any("reversal not measured" in reason for reason in result["reasons"])

    written = {call.args[1]: call.args[2] for call in m_write.call_args_list}
    assert written["baseline-reversal-status"]["measured"] is False
    assert "sysbench-baseline-reversal" not in written
    assert (
        result["attestation"]["benchmark_artifacts"]["baseline_reversal_status"]
        == "/tmp/artifact.json"
    )


def test_ignored_error_mismatch_is_warning_only(
    budget, profile, plan, staging_cfg
):
    baseline = _measurement([100.0, 100.0], ignored_errors=20)
    tuned = _measurement([112.0, 110.0], ignored_errors=38)
    reversal = _measurement([100.0, 100.0], ignored_errors=20)

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned, reversal],
        ),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(
            f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"
        ) as m_write,
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
        )

    # The throughput comparison itself passes, but the error-RATE parity veto
    # fires: tuned rate 38/222 ~= 17.1% exceeds pooled-baseline rate 40/400
    # = 10% by >50% with a material absolute gap (38 vs A1 20), so the win
    # does not stand. The count mismatch warning is still recorded.
    assert result["paired"]["status"] == "PASS"
    assert result["status"] == "FAIL"
    assert result["ignored_errors"]["mismatch"] is True
    assert result["ignored_errors"]["baseline_first"] == 20
    assert result["ignored_errors"]["baseline_reversal"] == 20
    assert result["ignored_errors"]["tuned"] == 38
    assert any("ignored-error mismatch" in reason for reason in result["reasons"])
    assert any(
        reason.startswith("warning:") and "ignored-error mismatch" in reason
        for reason in result["reasons"]
    )
    assert any("tuned error rate" in reason for reason in result["reasons"])

    written = {call.args[1]: call.args[2] for call in m_write.call_args_list}
    assert written["ignored-errors"]["mismatch"] is True
    assert result["attestation"]["benchmark_artifacts"]["ignored_errors"] == (
        "/tmp/artifact.json"
    )


def _snapshot_key(profile: SysbenchProfile) -> str:
    return SnapshotRegistry.key_for(
        "postgres", "17", profile.tables, profile.rows_per_table
    )


def test_reversal_disabled_skips_reversal(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned],
        ) as m_bench,
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
        patch(f"{_VALIDATION}._collect_pg_write_stats", return_value={}),
        patch(f"{_VALIDATION}._measure_baseline_reversal") as m_rev,
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
            reversal=False,
        )

    assert m_bench.call_count == 2
    m_rev.assert_not_called()
    assert result["baseline_reversal"]["measured"] is False
    assert "disabled" in result["baseline_reversal"]["reason"]


def test_snapshot_hit_boots_from_image_and_reuses_dataset(
    budget, profile, plan, staging_cfg
):
    baseline = _measurement([100.0, 100.0])
    tuned = _measurement([112.0, 110.0])
    reversal = _measurement([100.0, 100.0])
    registry = SnapshotRegistry()
    image = "adco-staging-ready:run1-1x10"
    registry.register(_snapshot_key(profile), image)

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ) as m_start,
        patch(
            f"{_VALIDATION}.recreate_docker_db",
            return_value=(True, "stg-new", staging_cfg),
        ) as m_rec,
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned, reversal],
        ) as m_bench,
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
        patch(f"{_VALIDATION}._collect_pg_write_stats", return_value={}),
    ):
        validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
            snapshot=registry,
        )

    assert m_start.call_args.kwargs.get("base_image") == image
    assert [c.kwargs["prepare"] for c in m_bench.call_args_list] == [
        False,
        False,
        False,
    ]
    # Mutating tuned and reversal arms restore from the snapshot instead of
    # re-running prepare.
    assert m_rec.call_count == 2
    assert m_rec.call_args_list[0].kwargs.get("base_image") == image


def test_snapshot_miss_commits_clean_dataset_then_recreates_arms(
    budget, profile, plan, staging_cfg
):
    measurements = iter(
        [
            _measurement([100.0, 100.0]),
            _measurement([112.0, 110.0]),
            _measurement([100.0, 100.0]),
        ]
    )
    registry = SnapshotRegistry()
    committed: dict[str, str] = {}

    def fake_runner(
        cfg, prof, workdir=None, progress=None, prepare=True, on_rep=None, on_prepared=None
    ):
        if prepare and on_prepared is not None:
            on_prepared()
        return next(measurements)

    def fake_commit(container, image_name):
        committed["image"] = image_name
        return True, "ok"

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(f"{_VALIDATION}.commit_staging_db", side_effect=fake_commit),
        patch(
            f"{_VALIDATION}.recreate_docker_db",
            return_value=(True, "stg-new", staging_cfg),
        ) as m_rec,
        patch(f"{_VALIDATION}.run_sysbench_measurement", side_effect=fake_runner) as m_bench,
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
        patch(f"{_VALIDATION}._collect_pg_write_stats", return_value={}),
    ):
        validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
            snapshot=registry,
        )

    assert committed["image"].startswith("adco-staging-ready:")
    assert registry.get(_snapshot_key(profile)) == committed["image"]
    assert [c.kwargs["prepare"] for c in m_bench.call_args_list] == [
        True,
        False,
        False,
    ]
    assert m_rec.call_count == 2


def test_snapshot_commit_failure_falls_back_to_prepare(
    budget, profile, plan, staging_cfg
):
    measurements = iter(
        [
            _measurement([100.0, 100.0]),
            _measurement([112.0, 110.0]),
            _measurement([100.0, 100.0]),
        ]
    )
    registry = SnapshotRegistry()

    def fake_runner(
        cfg, prof, workdir=None, progress=None, prepare=True, on_rep=None, on_prepared=None
    ):
        if prepare and on_prepared is not None:
            on_prepared()
        return next(measurements)

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.commit_staging_db",
            return_value=(False, "disk full"),
        ),
        patch(f"{_VALIDATION}.recreate_docker_db") as m_rec,
        patch(f"{_VALIDATION}.run_sysbench_measurement", side_effect=fake_runner) as m_bench,
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
        patch(f"{_VALIDATION}._collect_pg_write_stats", return_value={}),
    ):
        validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
            snapshot=registry,
        )

    assert registry.images() == []
    assert [c.kwargs["prepare"] for c in m_bench.call_args_list] == [
        True,
        True,
        True,
    ]
    m_rec.assert_not_called()


def test_baseline_only_measures_without_applying_plan(budget, profile, plan, staging_cfg):
    baseline = _measurement([100.0] * 10)
    with (
        patch(f"{_VALIDATION}.start_staging_db", return_value=("stg-container", staging_cfg)),
        patch(f"{_VALIDATION}.run_sysbench_measurement", return_value=baseline) as m_bench,
        patch(f"{_VALIDATION}.apply_knobs") as m_apply,
        patch(f"{_VALIDATION}.restart_docker_db") as m_restart,
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
            baseline_only=True,
        )

    assert result["status"] == "ok"
    assert result["baseline"].tps == baseline.tps
    assert len(result["baseline"].per_run_tps) == 10
    m_bench.assert_called_once()
    m_apply.assert_not_called()
    m_restart.assert_not_called()


def test_shared_baseline_skips_baseline_and_reversal(budget, profile, plan, staging_cfg):
    shared = _measurement([100.0, 100.0], ignored_errors=20)
    tuned = _measurement([112.0, 110.0], ignored_errors=38)
    with (
        patch(f"{_VALIDATION}.start_staging_db", return_value=("stg-container", staging_cfg)),
        patch(
            f"{_VALIDATION}.run_sysbench_measurement", return_value=tuned
        ) as m_bench,
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
            shared_baseline=shared,
            early_stop_min_reps=4,
        )

    # Only the tuned arm runs: no internal baseline, no reversal.
    # The throughput comparison passes but the error-rate parity veto fires
    # (tuned 38/222 ~= 17.1% vs shared-baseline 20/200 = 10%), so FAIL.
    m_bench.assert_called_once()
    assert result["status"] == "FAIL"
    assert result["stopped_early"] is False
    assert result["ignored_errors"]["mismatch"] is True
    assert any(
        reason.startswith("warning:") and "ignored-error mismatch" in reason
        for reason in result["reasons"]
    )
    assert any("tuned error rate" in reason for reason in result["reasons"])
    assert result["baseline_reversal"]["measured"] is False
    assert result["baseline_reversal"]["reason"] == (
        "shared baseline reused across candidates"
    )


def test_early_stop_futility_short_circuits_loser(budget, profile, plan, staging_cfg):
    shared = _measurement([100.0] * 10)

    def fake_runner(cfg, prof, workdir, progress=None, prepare=True, on_rep=None, on_prepared=None):
        assert on_rep is not None
        collected: list[float] = []
        for sample in (70.0, 72.0, 71.0, 69.0, 68.0, 67.0):
            collected.append(sample)
            if on_rep(list(collected)):
                break
        return _measurement(collected)

    with (
        patch(f"{_VALIDATION}.start_staging_db", return_value=("stg-container", staging_cfg)),
        patch(f"{_VALIDATION}.run_sysbench_measurement", side_effect=fake_runner),
        patch(f"{_VALIDATION}.apply_knobs", return_value=_apply_results()),
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()),
        patch(f"{_VALIDATION}.snapshot_settings", side_effect=_settings_seq()),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")),
    ):
        result = validate_plan(
            **_call_kwargs(budget, profile, plan),
            apply_mode=ApplyMode.LIVE,
            shared_baseline=shared,
            early_stop_min_reps=4,
        )

    assert result["stopped_early"] is True
    assert len(result["paired"]["tuned"]["per_run_tps"]) == 4
    # A futility-stopped loser is never reported as a win.
    assert result["status"] == "FAIL"
