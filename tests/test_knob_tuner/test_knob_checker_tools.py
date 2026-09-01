"""Unit tests for knob_checker sub-agent models, agent creation, and tools."""

import os
import tempfile
from unittest.mock import MagicMock, patch
import pytest

from src.knob_tuner.sub_agents.knob_checker.agent import (
    create_knob_checker_agent,
)
from src.knob_tuner.sub_agents.knob_checker.models import (
    KnobCheckIssue,
    KnobCheckerOutput,
)
from src.knob_tuner.sub_agents.knob_checker.tools import (
    _get_staging_db_config,
    apply_knobs_staging,
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
    assert len(agent.tools) == 3
    tool_names = [t.__name__ for t in agent.tools]
    assert "apply_knobs_staging" in tool_names
    assert "restart_database_staging" in tool_names
    assert "test_database_staging" in tool_names


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
    assert "Container restarted" in result


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
