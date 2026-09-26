"""Unit tests for the knob_tuner pydantic contracts."""

import pytest
from pydantic import ValidationError

from src.knob_tuner.contracts import (
    KnobPlan,
    KnobScope,
    KnobSpec,
    PairedResult,
    ResourceBudget,
    SysbenchMeasurement,
    SysbenchProfile,
    TuningStatus,
)


def _measurement(
    tps: float = 100.0,
    p95: float = 10.0,
    per_run: list[float] | None = None,
    status: str = "ok",
    ignored_errors: int = 0,
    reconnects: int = 0,
) -> SysbenchMeasurement:
    if per_run is None:
        per_run = [tps, tps, tps]
    return SysbenchMeasurement(
        status=status,
        tps=tps,
        latency_p95_ms=p95,
        per_run_tps=per_run,
        ignored_errors=ignored_errors,
        reconnects=reconnects,
    )


@pytest.mark.parametrize(
    "cpu_cores,memory_gb",
    [
        (0, 4.0),
        (-1, 4.0),
        (4, 0.0),
        (4, -2.0),
        (True, 4.0),
        (4, True),
        (None, 4.0),
        (4, None),
        ("auto", 4.0),
        (4, "auto"),
    ],
)
def test_resource_budget_rejects_invalid(cpu_cores, memory_gb):
    with pytest.raises(ValidationError):
        ResourceBudget(cpu_cores=cpu_cores, memory_gb=memory_gb)


def test_resource_budget_docker_formatting():
    budget = ResourceBudget(cpu_cores=4, memory_gb=8.0)
    assert budget.to_docker_cpus() == "4"
    assert budget.to_docker_memory() == "8g"
    assert budget.to_dict() == {"cpu_cores": 4, "memory_gb": 8.0}


def test_resource_budget_fractional_memory():
    budget = ResourceBudget(cpu_cores=2, memory_gb=8.5)
    assert budget.to_docker_memory() == "8.5g"


def test_sysbench_profile_defaults():
    profile = SysbenchProfile()
    assert profile.profile_type == "oltp_read_write"
    assert profile.tables == 10
    assert profile.rows_per_table == 10000
    assert profile.threads == 4
    assert profile.warmup_seconds == 10
    assert profile.measurement_seconds == 30
    assert profile.repetitions == 3
    assert profile.seed == 42
    assert profile.throughput_threshold_pct == 0.0
    assert profile.latency_threshold_pct == 5.0


def test_sysbench_profile_hash_is_stable():
    assert SysbenchProfile().profile_hash() == SysbenchProfile().profile_hash()


def test_sysbench_profile_hash_changes_with_field():
    base = SysbenchProfile()
    changed = SysbenchProfile(threads=8)
    assert base.profile_hash() != changed.profile_hash()


def test_knob_plan_hash_is_stable_and_order_independent():
    plan_a = KnobPlan(
        knobs=[
            KnobSpec(name="shared_buffers", value="256MB", scope=KnobScope.SIGHUP),
            KnobSpec(name="work_mem", value="8MB", scope=KnobScope.USER),
        ]
    )
    plan_b = KnobPlan(
        knobs=[
            KnobSpec(name="work_mem", value="8MB", scope=KnobScope.USER),
            KnobSpec(name="shared_buffers", value="256MB", scope=KnobScope.SIGHUP),
        ]
    )
    assert plan_a.plan_hash() == plan_b.plan_hash()
    assert len(plan_a.plan_hash()) == 64


def test_knob_plan_hash_changes_on_value_and_scope():
    base = KnobPlan(knobs=[KnobSpec(name="work_mem", value="8MB")])
    value_changed = KnobPlan(knobs=[KnobSpec(name="work_mem", value="16MB")])
    scope_changed = KnobPlan(
        knobs=[KnobSpec(name="work_mem", value="8MB", scope=KnobScope.USER)]
    )
    assert base.plan_hash() != value_changed.plan_hash()
    assert base.plan_hash() != scope_changed.plan_hash()


def test_paired_result_pass():
    profile = SysbenchProfile()
    result = PairedResult.evaluate(
        _measurement(tps=100.0), _measurement(tps=105.0), profile
    )
    assert result.status == TuningStatus.PASS
    assert result.reasons == []
    assert result.delta_pct == pytest.approx(5.0)
    assert result.median_tps_baseline == 100.0
    assert result.median_tps_tuned == 105.0


def test_paired_result_fail_throughput_drop():
    profile = SysbenchProfile()
    result = PairedResult.evaluate(
        _measurement(tps=100.0), _measurement(tps=80.0), profile
    )
    assert result.status == TuningStatus.FAIL
    assert any("TPS" in reason for reason in result.reasons)


def test_paired_result_fail_latency():
    profile = SysbenchProfile(latency_threshold_pct=5.0)
    result = PairedResult.evaluate(
        _measurement(tps=100.0, p95=10.0),
        _measurement(tps=100.0, p95=20.0),
        profile,
    )
    assert result.status == TuningStatus.FAIL
    assert any("p95" in reason for reason in result.reasons)


def test_paired_result_inconclusive_error_status():
    profile = SysbenchProfile()
    result = PairedResult.evaluate(
        _measurement(tps=100.0, status="error"), _measurement(tps=105.0), profile
    )
    assert result.status == TuningStatus.INCONCLUSIVE
    assert result.reasons


def test_paired_result_inconclusive_zero_tps():
    profile = SysbenchProfile()
    result = PairedResult.evaluate(
        _measurement(tps=100.0),
        _measurement(tps=0.0, per_run=[0.0, 0.0, 0.0]),
        profile,
    )
    assert result.status == TuningStatus.INCONCLUSIVE
    assert any("zero TPS" in reason for reason in result.reasons)


def test_paired_result_inconclusive_too_few_repetitions():
    profile = SysbenchProfile()
    result = PairedResult.evaluate(
        _measurement(tps=100.0, per_run=[100.0, 100.0]),
        _measurement(tps=105.0),
        profile,
    )
    assert result.status == TuningStatus.INCONCLUSIVE
    assert any("repetitions" in reason for reason in result.reasons)


def test_paired_result_ignored_errors_warn_but_reconnects_invalidate():
    profile = SysbenchProfile()
    with_errors = PairedResult.evaluate(
        _measurement(tps=100.0, ignored_errors=1),
        _measurement(tps=105.0),
        profile,
    )
    with_reconnects = PairedResult.evaluate(
        _measurement(tps=100.0),
        _measurement(tps=105.0, reconnects=2),
        profile,
    )
    # Ignored errors are recorded as a warning but do not invalidate evidence.
    assert with_errors.status == TuningStatus.PASS
    assert any("warning" in reason for reason in with_errors.reasons)
    # Reconnects remain fatal.
    assert with_reconnects.status == TuningStatus.INCONCLUSIVE


def test_paired_result_never_pass_without_evidence():
    profile = SysbenchProfile()
    result = PairedResult.evaluate(
        SysbenchMeasurement(), SysbenchMeasurement(), profile
    )
    assert result.status == TuningStatus.INCONCLUSIVE
