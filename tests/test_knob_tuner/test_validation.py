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
from src.knob_tuner.tools.validation import validate_plan

_VALIDATION = "src.knob_tuner.tools.validation"


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


def _measurement(per_run_tps: list[float]) -> SysbenchMeasurement:
    return SysbenchMeasurement(
        status="ok",
        tps=sum(per_run_tps) / len(per_run_tps),
        qps=sum(per_run_tps) / len(per_run_tps),
        latency_avg_ms=1.0,
        latency_p95_ms=2.0,
        ignored_errors=0,
        reconnects=0,
        threads=1,
        tables=1,
        rows_per_table=10,
        duration=1,
        seed=42,
        repetitions=len(per_run_tps),
        per_run_tps=list(per_run_tps),
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
            apply_mode=ApplyMode.DYNAMIC,
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
    before = {"work_mem": "4096", "shared_buffers": "16384"}
    after = {"work_mem": "65536", "shared_buffers": "1048576"}

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ) as m_start,
        patch(
            f"{_VALIDATION}.run_sysbench_measurement",
            side_effect=[baseline, tuned],
        ) as m_bench,
        patch(
            f"{_VALIDATION}.apply_knobs", return_value=_apply_results()
        ) as m_apply,
        patch(f"{_VALIDATION}.verify_active_knobs", return_value=_verify()) as m_verify,
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
            apply_mode=ApplyMode.DYNAMIC,
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
    m_apply.assert_called_once()
    m_restart.assert_not_called()
    m_stop.assert_called_once_with("stg-container")
    assert m_snapshot.call_count == 2

    written_names = [call.args[1] for call in m_write.call_args_list]
    assert "sysbench-baseline" in written_names
    assert "sysbench-tuned" in written_names
    assert "settings-after" in written_names
    assert "validation-attempt-1" in written_names


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
            apply_mode=ApplyMode.DYNAMIC,
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
            apply_mode=ApplyMode.DYNAMIC,
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
            apply_mode=ApplyMode.DYNAMIC,
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
            apply_mode=ApplyMode.DYNAMIC,
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
            apply_mode=ApplyMode.PERSIST_STATIC,
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
            apply_mode=ApplyMode.PERSIST_STATIC,
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
            apply_mode=ApplyMode.DYNAMIC,
            progress=messages.append,
        )

    assert result["status"] == "PASS"
    joined = "\n".join(messages)
    assert "provisioning staging" in joined
    assert "applying" in joined
    assert "verifying" in joined
    assert "verdict:" in joined
