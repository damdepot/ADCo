"""Symmetric restore for the shared baseline.

Production-observed bias: the shared-baseline arm measured on the container
that just did the fresh bulk prepare, while every tuned arm measured on a
container booted from the snapshot image. Baseline rep 1 dipped (cold /
analyzing / checkpoint aftermath) while tuned rep 1s never dipped.

Fix: the shared baseline first loads+settles+commits the snapshot via a
``prepare_only`` runner call (no reps), then recreates from the snapshot
exactly like tuned arms, then measures (``prepare=False``) on the fresh
container. Commit/restore failures fall back to measuring on the preparing
container and never fail the run.
"""

import inspect
from unittest.mock import MagicMock, patch

from src.knob_tuner.contracts import (
    ApplyMode,
    KnobPlan,
    ResourceBudget,
    SysbenchMeasurement,
    SysbenchProfile,
)
from src.knob_tuner.tools.benchmark_tools import (
    run_pgbench_measurement,
    run_sysbench_measurement,
)
from src.knob_tuner.tools.db_connector import DBConfig

_VALIDATION = "src.knob_tuner.tools.validation"
_RUN_PATCH = "src.knob_tuner.tools.benchmark_tools.subprocess.run"
_DB_CONN_PATCH = "src.knob_tuner.tools.db_connector.get_connection"


def _budget() -> ResourceBudget:
    return ResourceBudget(cpu_cores=2, memory_gb=2)


def _profile() -> SysbenchProfile:
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


def _staging_cfg(port: int = 55000) -> DBConfig:
    return DBConfig(
        host="127.0.0.1",
        port=port,
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
        error=None,
    )


def _empty_prep_result() -> SysbenchMeasurement:
    """The discarded no-rep ``prepare_only`` result (reps live on the restore)."""
    return SysbenchMeasurement(
        status="ok",
        threads=1,
        tables=1,
        rows_per_table=10,
        duration=1,
        seed=42,
        repetitions=2,
        per_run_tps=[],
        prepare_seconds=1.5,
        log_file="/tmp/prep.log",
        error=None,
    )


def _baseline_only_kwargs(budget, profile, tmp_path):
    from src.knob_tuner.tools.validation import SnapshotRegistry  # noqa: F401

    return dict(
        run_id="run-1",
        run_dir=str(tmp_path),
        plan=KnobPlan(knobs=[]),
        budget=budget,
        profile=profile,
        db_type="postgres",
        db_version="17",
        database="testdb",
        apply_mode=ApplyMode.LIVE,
        baseline_only=True,
    )


def _recording_runner(results: list[SysbenchMeasurement]) -> MagicMock:
    """Runner mock that fires ``on_prepared`` on prepare arms, like the real one."""
    pending = list(results)

    def _fake(
        cfg,
        prof,
        workdir=None,
        progress=None,
        prepare=True,
        prepare_only=False,
        on_prepared=None,
        on_rep=None,
    ):
        if prepare and on_prepared is not None:
            on_prepared()
        return pending.pop(0)

    return MagicMock(side_effect=_fake)


def test_shared_baseline_happy_path_measures_on_restored_container(tmp_path):
    """prepare_only -> recreate -> official baseline is the SECOND result."""
    from src.knob_tuner.tools.validation import SnapshotRegistry, validate_plan

    budget, profile = _budget(), _profile()
    staging_cfg = _staging_cfg(55000)
    restored_cfg = _staging_cfg(55001)
    official = _measurement([100.0, 100.0])
    registry = SnapshotRegistry()
    runner = _recording_runner([_empty_prep_result(), official])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(
            f"{_VALIDATION}.commit_staging_db", return_value=(True, "ok")
        ) as m_commit,
        patch(
            f"{_VALIDATION}.recreate_docker_db",
            return_value=(True, "stg-new", restored_cfg),
        ) as m_rec,
        patch(f"{_VALIDATION}.run_sysbench_measurement", runner),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")) as m_stop,
    ):
        result = validate_plan(
            **_baseline_only_kwargs(budget, profile, tmp_path),
            snapshot=registry,
        )

    assert result["status"] == "ok"
    # The official baseline is the SECOND call's result (measured on restore).
    assert result["baseline"].per_run_tps == [100.0, 100.0]
    assert result["baseline"].tps == official.tps

    assert runner.call_count == 2
    first, second = runner.call_args_list
    assert first.kwargs["prepare"] is True
    assert first.kwargs["prepare_only"] is True
    assert first.kwargs["on_prepared"] is not None
    assert second.kwargs["prepare"] is False
    assert second.kwargs.get("prepare_only", False) is False
    # A container recreate happened between the two runner calls...
    assert m_commit.called
    image = registry.get(
        SnapshotRegistry.key_for("postgres", "17", profile.tables, profile.rows_per_table)
    )
    assert image
    assert m_rec.call_count == 1
    assert m_rec.call_args.kwargs.get("base_image") == image
    # ...and the official measurement ran against the restored container.
    assert second.args[0] == restored_cfg

    timings = result["artifacts"]["timings"]
    assert "restore_seconds" in timings
    assert "provision_seconds" in timings
    assert "baseline_seconds" in timings
    assert "prepare_seconds" in timings
    # Teardown stops whichever container is current (the restored one).
    m_stop.assert_called_once_with("stg-new")
    assert result["snapshot_image"] == image


def test_shared_baseline_commit_failure_falls_back_to_preparing_container(tmp_path):
    """No snapshot image -> no recreate; baseline measured on preparing container."""
    from src.knob_tuner.tools.validation import SnapshotRegistry, validate_plan

    budget, profile = _budget(), _profile()
    staging_cfg = _staging_cfg()
    official = _measurement([100.0, 100.0])
    registry = SnapshotRegistry()
    runner = _recording_runner([_empty_prep_result(), official])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(f"{_VALIDATION}.commit_staging_db", return_value=(False, "disk full")),
        patch(f"{_VALIDATION}.recreate_docker_db") as m_rec,
        patch(f"{_VALIDATION}.run_sysbench_measurement", runner),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")) as m_stop,
    ):
        result = validate_plan(
            **_baseline_only_kwargs(budget, profile, tmp_path),
            snapshot=registry,
        )

    # The run never fails because of the snapshot path.
    assert result["status"] == "ok"
    assert result["baseline"].per_run_tps == [100.0, 100.0]
    assert registry.images() == []
    # Nothing to restore from, so no recreate is attempted...
    m_rec.assert_not_called()
    # ...and the official measurement stays on the preparing container.
    assert runner.call_count == 2
    assert runner.call_args_list[1].kwargs["prepare"] is False
    assert runner.call_args_list[1].args[0] == staging_cfg
    assert "restore_seconds" not in result["artifacts"]["timings"]
    m_stop.assert_called_once_with("stg-container")


def test_shared_baseline_restore_failure_falls_back_to_preparing_container(tmp_path):
    """Recreate failure -> measure on the preparing container, never throw."""
    from src.knob_tuner.tools.validation import SnapshotRegistry, validate_plan

    budget, profile = _budget(), _profile()
    staging_cfg = _staging_cfg()
    official = _measurement([100.0, 100.0])
    registry = SnapshotRegistry()
    runner = _recording_runner([_empty_prep_result(), official])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(f"{_VALIDATION}.commit_staging_db", return_value=(True, "ok")),
        patch(
            f"{_VALIDATION}.recreate_docker_db",
            return_value=(False, "docker daemon hiccup"),
        ) as m_rec,
        patch(f"{_VALIDATION}.run_sysbench_measurement", runner),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")) as m_stop,
    ):
        result = validate_plan(
            **_baseline_only_kwargs(budget, profile, tmp_path),
            snapshot=registry,
        )

    assert result["status"] == "ok"
    assert result["baseline"].per_run_tps == [100.0, 100.0]
    assert m_rec.call_count == 1
    assert runner.call_count == 2
    assert runner.call_args_list[1].args[0] == staging_cfg
    assert "restore_seconds" not in result["artifacts"]["timings"]
    m_stop.assert_called_once_with("stg-container")


def test_shared_baseline_restore_exception_falls_back_without_throwing(tmp_path):
    """A raising recreate is suppressed with a reason string, like the tuned path."""
    from src.knob_tuner.tools.validation import SnapshotRegistry, validate_plan

    budget, profile = _budget(), _profile()
    staging_cfg = _staging_cfg()
    official = _measurement([100.0, 100.0])
    registry = SnapshotRegistry()
    runner = _recording_runner([_empty_prep_result(), official])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(f"{_VALIDATION}.commit_staging_db", return_value=(True, "ok")),
        patch(
            f"{_VALIDATION}.recreate_docker_db",
            side_effect=RuntimeError("daemon gone"),
        ),
        patch(f"{_VALIDATION}.run_sysbench_measurement", runner),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")) as m_stop,
    ):
        result = validate_plan(
            **_baseline_only_kwargs(budget, profile, tmp_path),
            snapshot=registry,
        )

    assert result["status"] == "ok"
    assert result["baseline"].per_run_tps == [100.0, 100.0]
    m_stop.assert_called_once_with("stg-container")


def test_shared_baseline_legacy_runner_without_prepare_only_still_measures(tmp_path):
    """A runner with the old signature falls back to the single-container call."""
    from src.knob_tuner.tools.validation import SnapshotRegistry, validate_plan

    budget, profile = _budget(), _profile()
    staging_cfg = _staging_cfg()
    official = _measurement([100.0, 100.0])
    registry = SnapshotRegistry()

    def _legacy_runner(
        cfg, prof, workdir=None, progress=None, prepare=True, on_prepared=None, on_rep=None
    ):
        if prepare and on_prepared is not None:
            on_prepared()
        return official

    runner = MagicMock(side_effect=_legacy_runner)

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(f"{_VALIDATION}.commit_staging_db", return_value=(True, "ok")),
        patch(f"{_VALIDATION}.recreate_docker_db") as m_rec,
        patch(f"{_VALIDATION}.run_sysbench_measurement", runner),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")) as m_stop,
    ):
        result = validate_plan(
            **_baseline_only_kwargs(budget, profile, tmp_path),
            snapshot=registry,
        )

    assert result["status"] == "ok"
    # The baseline is the legacy (single-container) call's result.
    assert result["baseline"].per_run_tps == [100.0, 100.0]
    # The successful measurement never carried prepare_only=True.
    successful = runner.call_args_list[-1]
    assert successful.kwargs.get("prepare_only", False) is False
    assert successful.kwargs["prepare"] is True
    m_rec.assert_not_called()
    m_stop.assert_called_once_with("stg-container")


def test_shared_baseline_prepare_failure_returns_error(tmp_path):
    """A failing prepare_only result takes the existing error path (no recreate)."""
    from src.knob_tuner.tools.validation import SnapshotRegistry, validate_plan

    budget, profile = _budget(), _profile()
    staging_cfg = _staging_cfg()
    failed = SysbenchMeasurement(status="error", error="prepare exploded")
    registry = SnapshotRegistry()
    runner = _recording_runner([failed])

    with (
        patch(
            f"{_VALIDATION}.start_staging_db",
            return_value=("stg-container", staging_cfg),
        ),
        patch(f"{_VALIDATION}.commit_staging_db", return_value=(True, "ok")),
        patch(f"{_VALIDATION}.recreate_docker_db") as m_rec,
        patch(f"{_VALIDATION}.run_sysbench_measurement", runner),
        patch(f"{_VALIDATION}.write_artifact", return_value="/tmp/artifact.json"),
        patch(f"{_VALIDATION}.stop_staging_db", return_value=(True, "stopped")) as m_stop,
    ):
        result = validate_plan(
            **_baseline_only_kwargs(budget, profile, tmp_path),
            snapshot=registry,
        )

    assert result["status"] == "error"
    assert result["baseline"].status == "error"
    assert any("shared baseline measurement" in reason for reason in result["reasons"])
    m_rec.assert_not_called()
    m_stop.assert_called_once_with("stg-container")


def test_runners_accept_prepare_only_kwarg():
    """Signature-level: both runners take ``prepare_only`` defaulting to False."""
    for runner in (run_sysbench_measurement, run_pgbench_measurement):
        param = inspect.signature(runner).parameters["prepare_only"]
        assert param.default is False


def _sysbench_calls(cmds: list[tuple[list[str], dict]]) -> list[list[str]]:
    return [cmd for cmd, _ in cmds if cmd[-1] == "run"]


def test_sysbench_prepare_only_skips_warmup_and_reps(mock_db_config_pg, tmp_path):
    """prepare_only loads+settles+snapshots, then returns before any warmup/rep."""
    from unittest.mock import MagicMock as _MagicMock

    from src.knob_tuner.tools import benchmark_tools as bt

    profile = SysbenchProfile(
        tables=1,
        rows_per_table=10,
        threads=1,
        warmup_seconds=5,
        measurement_seconds=10,
        repetitions=3,
        seed=7,
    )
    calls: list[tuple[list[str], dict]] = []

    def _side_effect(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _MagicMock(returncode=0, stdout="done", stderr="")

    on_prepared = MagicMock()
    with (
        patch(_DB_CONN_PATCH),
        patch(_RUN_PATCH, side_effect=_side_effect),
        patch.object(bt, "_settle_after_load") as settle,
    ):
        result = run_sysbench_measurement(
            mock_db_config_pg,
            profile,
            workdir=str(tmp_path),
            prepare=True,
            prepare_only=True,
            on_prepared=on_prepared,
        )

    assert result.status == "ok"
    assert result.error is None
    assert result.per_run_tps == []
    assert result.prepare_seconds >= 0.0
    on_prepared.assert_called_once()
    settle.assert_called_once()
    phases = [cmd[-1] for cmd, _ in calls]
    assert "cleanup" in phases
    assert "prepare" in phases
    assert _sysbench_calls(calls) == []


def test_sysbench_prepare_only_prepare_failure_is_error(mock_db_config_pg, tmp_path):
    """Prepare failures behave exactly as in a full measurement."""
    from unittest.mock import MagicMock as _MagicMock

    from src.knob_tuner.tools import benchmark_tools as bt

    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=1, repetitions=1)

    def _side_effect(cmd, **kwargs):
        if cmd[-1] == "prepare":
            return _MagicMock(returncode=1, stdout="", stderr="FATAL: Disk full")
        return _MagicMock(returncode=0, stdout="done", stderr="")

    with (
        patch(_DB_CONN_PATCH),
        patch(_RUN_PATCH, side_effect=_side_effect),
        patch.object(bt, "_settle_after_load"),
    ):
        result = run_sysbench_measurement(
            mock_db_config_pg,
            profile,
            workdir=str(tmp_path),
            prepare=True,
            prepare_only=True,
        )

    assert result.status == "error"
    assert "prepare failed" in result.error


def test_pgbench_prepare_only_skips_warmup_and_reps(mock_db_config_pg, tmp_path):
    """pgbench prepare_only loads+settles+snapshots without any pgbench -T run."""
    from unittest.mock import MagicMock as _MagicMock

    from src.knob_tuner.tools import benchmark_tools as bt

    profile = SysbenchProfile(
        tables=1,
        rows_per_table=100,
        threads=2,
        warmup_seconds=5,
        measurement_seconds=10,
        repetitions=3,
        seed=11,
    )
    calls: list[tuple[list[str], dict]] = []

    def _side_effect(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _MagicMock(returncode=0, stdout="done", stderr="")

    on_prepared = MagicMock()
    with (
        patch(_DB_CONN_PATCH),
        patch(_RUN_PATCH, side_effect=_side_effect),
        patch.object(bt, "_settle_after_load") as settle,
    ):
        result = run_pgbench_measurement(
            mock_db_config_pg,
            profile,
            workdir=str(tmp_path),
            prepare=True,
            prepare_only=True,
            on_prepared=on_prepared,
        )

    assert result.status == "ok"
    assert result.error is None
    assert result.per_run_tps == []
    assert result.prepare_seconds >= 0.0
    on_prepared.assert_called_once()
    settle.assert_called_once()
    phases = [cmd[-1] for cmd, _ in calls]
    assert "cleanup" in phases
    assert "prepare" in phases
    pgbench_cmds = [cmd for cmd, _ in calls if "pgbench" in " ".join(cmd) and "-T" in cmd]
    assert pgbench_cmds == []


def test_pgbench_prepare_only_prepare_failure_is_error(mock_db_config_pg, tmp_path):
    """pgbench prepare failures behave exactly as in a full measurement."""
    from unittest.mock import MagicMock as _MagicMock

    from src.knob_tuner.tools import benchmark_tools as bt

    profile = SysbenchProfile(warmup_seconds=0, measurement_seconds=10, repetitions=1)

    def _side_effect(cmd, **kwargs):
        if cmd[-1] == "prepare":
            return _MagicMock(returncode=1, stdout="", stderr="FATAL: Disk full")
        return _MagicMock(returncode=0, stdout="done", stderr="")

    with (
        patch(_DB_CONN_PATCH),
        patch(_RUN_PATCH, side_effect=_side_effect),
        patch.object(bt, "_settle_after_load"),
    ):
        result = run_pgbench_measurement(
            mock_db_config_pg,
            profile,
            workdir=str(tmp_path),
            prepare=True,
            prepare_only=True,
        )

    assert result.status == "error"
    assert "prepare failed" in result.error
