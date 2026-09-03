"""Unit tests for knob_checker sub-agent models, agent creation, and tools."""

import os
import tempfile
from unittest.mock import MagicMock, patch
import pytest

from src.knob_tuner.sub_agents.knob_checker.agent import (
    create_knob_checker_agent,
)
from src.knob_tuner.sub_agents.knob_checker.models import (
    BenchmarkResult,
    KnobCheckIssue,
    KnobCheckerOutput,
    StagingCheckDetails,
    StagingTestResults,
    SysbenchMetrics,
)
from src.knob_tuner.sub_agents.knob_checker.tools import (
    _get_staging_db_config,
    apply_knobs_staging,
    benchmark_baseline_staging,
    benchmark_tuned_staging,
    restart_database_staging,
    test_database_staging,
)
from src.knob_tuner.tools.db_connector import DBConfig


class MockToolContext:
    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}


# ===========================================================================
# 1. Pydantic Models Validation Tests
# ===========================================================================

def test_knob_check_issue_model_validates():
    issue = KnobCheckIssue(
        knob="shared_buffers",
        severity="critical",
        category="crash",
        description="PostgreSQL server failed to allocate shared memory segments (OOM on boot)",
        suggestion="Reduce shared_buffers from 16GB to 4GB",
    )
    assert issue.knob == "shared_buffers"
    assert issue.severity == "critical"
    assert issue.category == "crash"
    assert issue.suggestion == "Reduce shared_buffers from 16GB to 4GB"
    dump = issue.model_dump()
    assert KnobCheckIssue.model_validate(dump).severity == "critical"


def test_knob_checker_output_pass():
    out = KnobCheckerOutput(
        status="PASS",
        issues=[],
        test_results={"status": "ok", "checks": {"connectivity": True, "ping": True, "crud": True}},
        summary="All staging verification checks passed successfully.",
    )
    assert out.status == "PASS"
    assert len(out.issues) == 0


def test_knob_checker_output_fail():
    issue = KnobCheckIssue(
        knob="max_connections",
        severity="high",
        category="connectivity_failure",
        description="Connection refused after applying parameter",
    )
    out = KnobCheckerOutput(
        status="FAIL",
        issues=[issue],
        summary="Database became unreachable after applying new parameters.",
    )
    assert out.status == "FAIL"
    assert len(out.issues) == 1
    assert out.issues[0].severity == "high"


# ===========================================================================
# 2. Agent Factory Test
# ===========================================================================

def test_create_knob_checker_agent():
    agent = create_knob_checker_agent()
    assert agent.name == "knob_checker"
    assert agent.output_key == "knob_checker_output"
    assert agent.output_schema == KnobCheckerOutput
    assert len(agent.tools) == 5
    tool_names = [t.__name__ for t in agent.tools]
    assert "benchmark_baseline_staging" in tool_names
    assert "apply_knobs_staging" in tool_names
    assert "restart_database_staging" in tool_names
    assert "test_database_staging" in tool_names
    assert "benchmark_tuned_staging" in tool_names


# ===========================================================================
# 3. _get_staging_db_config Helper Tests
# ===========================================================================

def test_get_staging_db_config_from_staging_key():
    cfg = DBConfig(host="10.0.0.1", port=5432, user="u", password="p", database="d", db_type="postgres", env="staging")
    tc = MockToolContext({"staging_db_config": cfg})
    assert _get_staging_db_config(tc) is cfg


def test_get_staging_db_config_from_dict():
    cfg_dict = {"host": "stg.host", "port": 3306, "user": "root", "password": "x", "database": "stg_db", "db_type": "mysql"}
    tc = MockToolContext({"staging_db_config": cfg_dict})
    parsed = _get_staging_db_config(tc)
    assert parsed is not None
    assert parsed.host == "stg.host"
    assert parsed.db_type == "mysql"


def test_get_staging_db_config_missing():
    tc = MockToolContext({})
    assert _get_staging_db_config(tc) is None


# ===========================================================================
# 4. apply_knobs_staging Tool Tests
# ===========================================================================

@patch("src.knob_tuner.sub_agents.knob_checker.tools.apply_knobs")
def test_apply_knobs_staging_success(mock_apply, mock_db_config_pg):
    mock_apply.return_value = [
        {"knob": "shared_buffers", "value": "256MB", "status": "applied", "error": None}
    ]
    tc = MockToolContext({
        "staging_db_config": mock_db_config_pg,
        "selected_knobs": [{"knob": "shared_buffers", "recommended_value": "256MB"}],
    })

    result = apply_knobs_staging(tc)

    assert "Staging Knob Application Summary: 1/1 applied" in result
    assert "shared_buffers" in result
    assert "APPLIED" in result
    assert "staging_applied_knobs" in tc.state
    mock_apply.assert_called_once()


@patch("src.knob_tuner.sub_agents.knob_checker.tools.apply_knobs")
def test_apply_knobs_staging_with_failures(mock_apply, mock_db_config_pg):
    mock_apply.return_value = [
        {"knob": "invalid_param", "value": "100", "status": "failed", "error": "unrecognized parameter"}
    ]
    tc = MockToolContext({
        "staging_db_config": mock_db_config_pg,
        "selected_knobs": [{"knob": "invalid_param", "recommended_value": "100"}],
    })

    result = apply_knobs_staging(tc)

    assert "0/1 applied successfully, 1 failed" in result
    assert "FAILED" in result
    assert "unrecognized parameter" in result


def test_apply_knobs_staging_missing_config():
    tc = MockToolContext({"selected_knobs": [{"knob": "x", "value": 1}]})
    result = apply_knobs_staging(tc)
    assert "ERROR: Staging DBConfig not found in state" in result


def test_apply_knobs_staging_no_knobs(mock_db_config_pg):
    tc = MockToolContext({"staging_db_config": mock_db_config_pg})
    result = apply_knobs_staging(tc)
    assert "ERROR: No selected knob recommendations found" in result


# ===========================================================================
# 5. restart_database_staging Tool Tests
# ===========================================================================

@patch("src.knob_tuner.sub_agents.knob_checker.tools.restart_db_by_config")
def test_restart_database_staging_success(mock_restart, mock_db_config_pg):
    mock_restart.return_value = (True, "Container restarted")
    tc = MockToolContext({"staging_db_config": mock_db_config_pg})

    result = restart_database_staging(tc)
    assert "OK: Staging database restarted successfully" in result


@patch("src.knob_tuner.sub_agents.knob_checker.tools.restart_db_by_config")
def test_restart_database_staging_failure(mock_restart, mock_db_config_pg):
    mock_restart.return_value = (False, "Timeout waiting for docker container")
    tc = MockToolContext({"staging_db_config": mock_db_config_pg})

    result = restart_database_staging(tc)

    assert "ERROR: Staging database restart failed" in result
    assert "Timeout" in result


def test_restart_database_staging_missing_config():
    tc = MockToolContext({})
    result = restart_database_staging(tc)
    assert "ERROR: Staging DBConfig not found in state" in result


# ===========================================================================
# 6. test_database_staging Tool Tests
# ===========================================================================

@patch("src.knob_tuner.sub_agents.knob_checker.tools.test_database")
def test_test_database_staging_pass(mock_test_db, mock_db_config_pg):
    mock_test_db.return_value = {
        "status": "ok",
        "checks": {"connectivity": True, "ping": True, "table_scan": True, "crud": True},
        "details": {"tables_found": ["accounts"], "crud_result": "passed"},
        "error": None,
    }
    tc = MockToolContext({"staging_db_config": mock_db_config_pg})

    result = test_database_staging(tc)

    assert "Staging Database Option A Test Suite Result: **PASS**" in result
    assert "**Connectivity**: OK" in result
    assert "**CRUD Lifecycle**: OK" in result
    assert tc.state["staging_validated"] is True
    assert tc.state["staging_test_results"]["status"] == "ok"


@patch("src.knob_tuner.sub_agents.knob_checker.tools.test_database")
def test_test_database_staging_fail(mock_test_db, mock_db_config_pg):
    mock_test_db.return_value = {
        "status": "error",
        "checks": {"connectivity": True, "ping": True, "table_scan": True, "crud": False},
        "details": {"tables_found": ["accounts"], "crud_result": "failed"},
        "error": "CRUD test failed: inserted value did not match",
    }
    tc = MockToolContext({"staging_db_config": mock_db_config_pg})

    result = test_database_staging(tc)

    assert "Staging Database Option A Test Suite Result: **FAIL**" in result
    assert "**CRUD Lifecycle**: FAILED" in result
    assert "CRUD test failed" in result
    assert tc.state["staging_validated"] is False


def test_test_database_staging_missing_config():
    tc = MockToolContext({})
    result = test_database_staging(tc)
    assert "ERROR: Staging DBConfig not found in state" in result
    assert tc.state["staging_validated"] is False


# ===========================================================================
# 7. Benchmark Staging Tool Tests
# ===========================================================================

@patch("src.knob_tuner.sub_agents.knob_checker.tools.run_sysbench_benchmark")
def test_benchmark_baseline_staging_fresh(mock_run_bench, mock_db_config_pg):
    mock_run_bench.return_value = {
        "status": "ok",
        "tps": 200.0,
        "qps": 4000.0,
        "latency_avg_ms": 10.0,
        "latency_p95_ms": 15.0,
        "duration": 120,
        "threads": 32,
        "tables": 50,
        "log_file": "/tmp/baseline.log",
        "details": {},
    }
    tc = MockToolContext({"staging_db_config": mock_db_config_pg})

    result = benchmark_baseline_staging(tc)
    assert "Baseline Sysbench Benchmark Result: **OK**" in result
    assert "200.00" in result
    assert "4000.00" in result
    assert tc.state["staging_baseline_benchmark"]["tps"] == 200.0


def test_benchmark_baseline_staging_reuses_cached(mock_db_config_pg):
    cached_result = {
        "status": "ok",
        "tps": 220.0,
        "qps": 4400.0,
        "threads": 32,
        "tables": 50,
        "duration": 120,
        "log_file": "/tmp/cached_baseline.log",
        "details": {"latency_avg_ms": 9.5, "latency_95th_ms": 14.0},
    }
    tc = MockToolContext({
        "staging_db_config": mock_db_config_pg,
        "staging_baseline_benchmark": cached_result,
    })

    with patch("src.knob_tuner.sub_agents.knob_checker.tools.run_sysbench_benchmark") as mock_run:
        result = benchmark_baseline_staging(tc)
        mock_run.assert_not_called()
        assert "Cached" in result
        assert "220.00" in result


def test_benchmark_baseline_staging_missing_config():
    tc = MockToolContext({})
    result = benchmark_baseline_staging(tc)
    assert "ERROR: Staging DBConfig not found in state" in result


@patch("src.knob_tuner.sub_agents.knob_checker.tools.run_sysbench_benchmark")
def test_benchmark_baseline_staging_failure(mock_run_bench, mock_db_config_pg):
    mock_run_bench.return_value = {"status": "error", "error": "sysbench prepare failed"}
    tc = MockToolContext({"staging_db_config": mock_db_config_pg})

    result = benchmark_baseline_staging(tc)
    assert "WARNING: Sysbench baseline benchmark encountered an environment/tool issue" in result
    assert "ERROR/SKIPPED" in result
    assert "Candidate knobs have not been applied yet" in result
    # Verify candidate knobs are not blamed in staging_issues
    assert "staging_issues" not in tc.state


@patch("src.knob_tuner.sub_agents.knob_checker.tools.run_sysbench_benchmark")
def test_benchmark_baseline_staging_with_custom_state_params(mock_run_bench, mock_db_config_pg):
    mock_run_bench.return_value = {
        "status": "ok",
        "tps": 100.0,
        "qps": 2000.0,
        "duration": 60,
        "threads": 8,
        "tables": 20,
        "log_file": "/tmp/bench.log",
        "details": {},
    }
    tc = MockToolContext({
        "staging_db_config": mock_db_config_pg,
        "benchmark_tables": 20,
        "benchmark_table_size": 5000,
        "benchmark_threads": 8,
        "benchmark_duration": 60,
    })

    result = benchmark_baseline_staging(tc)
    assert "Baseline Sysbench Benchmark Result: **OK**" in result
    mock_run_bench.assert_called_once_with(
        mock_db_config_pg,
        tables=20,
        table_size=5000,
        threads=8,
        duration=60,
    )


@patch("src.knob_tuner.sub_agents.knob_checker.tools.run_sysbench_benchmark")
def test_benchmark_tuned_staging_pass(mock_run_bench, mock_db_config_pg):
    baseline_result = {"status": "ok", "tps": 200.0, "qps": 4000.0, "details": {}}
    mock_run_bench.return_value = {
        "status": "ok",
        "tps": 260.0,
        "qps": 5200.0,
        "duration": 120,
        "threads": 32,
        "tables": 50,
        "log_file": "/tmp/tuned.log",
        "details": {"latency_avg_ms": 7.5, "latency_95th_ms": 11.0},
    }
    tc = MockToolContext({
        "staging_db_config": mock_db_config_pg,
        "staging_baseline_benchmark": baseline_result,
        "staging_validated": True,
    })

    result = benchmark_tuned_staging(tc)
    assert "Result: **PASS**" in result
    assert "260.00" in result
    assert "+30.00%" in result
    assert tc.state["staging_benchmark_results"].status == "PASS"
    assert tc.state["staging_benchmark_results"].regression_detected is False
    assert tc.state["staging_validated"] is True


@patch("src.knob_tuner.sub_agents.knob_checker.tools.run_sysbench_benchmark")
def test_benchmark_tuned_staging_with_custom_state_params(mock_run_bench, mock_db_config_pg):
    baseline_result = {"status": "ok", "tps": 100.0, "qps": 2000.0, "details": {}}
    mock_run_bench.return_value = {
        "status": "ok",
        "tps": 150.0,
        "qps": 3000.0,
        "duration": 30,
        "threads": 2,
        "tables": 5,
        "log_file": "/tmp/tuned.log",
        "details": {},
    }
    tc = MockToolContext({
        "staging_db_config": mock_db_config_pg,
        "staging_baseline_benchmark": baseline_result,
        "staging_validated": True,
        "benchmark_tables": 5,
        "benchmark_table_size": 2000,
        "benchmark_threads": 2,
        "benchmark_duration": 30,
    })

    result = benchmark_tuned_staging(tc)
    assert "Result: **PASS**" in result
    mock_run_bench.assert_called_once_with(
        mock_db_config_pg,
        tables=5,
        table_size=2000,
        threads=2,
        duration=30,
    )


@patch("src.knob_tuner.sub_agents.knob_checker.tools.run_sysbench_benchmark")
def test_benchmark_tuned_staging_regression(mock_run_bench, mock_db_config_pg):
    baseline_result = {"status": "ok", "tps": 200.0, "qps": 4000.0, "details": {}}
    mock_run_bench.return_value = {
        "status": "ok",
        "tps": 160.0,
        "qps": 3200.0,
        "duration": 120,
        "threads": 32,
        "tables": 50,
        "log_file": "/tmp/tuned.log",
        "details": {"latency_avg_ms": 18.0, "latency_95th_ms": 35.0},
    }
    tc = MockToolContext({
        "staging_db_config": mock_db_config_pg,
        "staging_baseline_benchmark": baseline_result,
        "staging_validated": True,
    })

    result = benchmark_tuned_staging(tc)
    assert "Result: **REGRESSION**" in result
    assert "-20.00%" in result
    assert tc.state["staging_benchmark_results"].status == "REGRESSION"
    assert tc.state["staging_benchmark_results"].regression_detected is True
    assert tc.state["staging_validated"] is False
    issues = tc.state.get("staging_issues", [])
    assert any(i.category == "performance_regression" for i in issues)


def test_benchmark_tuned_staging_missing_config():
    tc = MockToolContext({})
    result = benchmark_tuned_staging(tc)
    assert "ERROR: Staging DBConfig not found in state" in result
    assert tc.state["staging_validated"] is False
