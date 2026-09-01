"""Unit tests for live_tuner sub-agent models, agent creation, and tools."""

from unittest.mock import MagicMock, patch
import pytest

from src.knob_tuner.sub_agents.live_tuner.agent import (
    create_live_tuner_agent,
)
from src.knob_tuner.sub_agents.live_tuner.models import (
    LiveTunerOutput,
)
from src.knob_tuner.sub_agents.live_tuner.tools import (
    _get_production_db_config,
    apply_knobs_production,
    check_staging_validation,
)
from src.knob_tuner.tools.db_connector import DBConfig


class MockToolContext:
    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}


# ===========================================================================
# 1. Pydantic Models Validation Tests
# ===========================================================================

def test_live_tuner_output_applied():
    out = LiveTunerOutput(
        status="APPLIED",
        applied_knobs=[{"knob": "work_mem", "value": "32MB", "status": "applied"}],
        restart_required_knobs=[{"knob": "shared_buffers", "value": "4GB", "reasoning": "25% RAM"}],
        summary="Dynamic knobs applied live; static knobs queued for maintenance window.",
    )
    assert out.status == "APPLIED"
    assert len(out.applied_knobs) == 1
    assert len(out.restart_required_knobs) == 1


def test_live_tuner_output_skipped():
    out = LiveTunerOutput(
        status="SKIPPED",
        skipped_reason="Staging validation failed.",
        summary="Aborted live deployment due to failed staging health check.",
    )
    assert out.status == "SKIPPED"
    assert "Staging" in out.skipped_reason


def test_live_tuner_output_partial_and_failed():
    out_partial = LiveTunerOutput(status="PARTIAL")
    out_failed = LiveTunerOutput(status="FAILED")
    assert out_partial.status == "PARTIAL"
    assert out_failed.status == "FAILED"


# ===========================================================================
# 2. Agent Factory Test
# ===========================================================================

def test_create_live_tuner_agent():
    agent = create_live_tuner_agent()
    assert agent.name == "live_tuner"
    assert agent.output_key == "live_tuner_output"
    assert agent.output_schema == LiveTunerOutput
    assert len(agent.tools) == 2
    tool_names = [t.__name__ for t in agent.tools]
    assert "check_staging_validation" in tool_names
    assert "apply_knobs_production" in tool_names


# ===========================================================================
# 3. _get_production_db_config Helper Tests
# ===========================================================================

def test_get_production_db_config_from_prod_key():
    cfg = DBConfig(host="prod.internal", port=5432, user="prod", password="x", database="db", db_type="postgres", env="production")
    tc = MockToolContext({"prod_db_config": cfg})
    assert _get_production_db_config(tc) is cfg


def test_get_production_db_config_from_dict():
    cfg_dict = {"host": "prod.mysql", "port": 3306, "user": "root", "password": "x", "database": "prod_db", "db_type": "mysql"}
    tc = MockToolContext({"prod_db_config": cfg_dict})
    parsed = _get_production_db_config(tc)
    assert parsed is not None
    assert parsed.host == "prod.mysql"
    assert parsed.env == "production"


def test_get_production_db_config_missing():
    tc = MockToolContext({})
    assert _get_production_db_config(tc) is None


# ===========================================================================
# 4. check_staging_validation Tool Tests
# ===========================================================================

def test_check_staging_validation_passed():
    tc = MockToolContext({"staging_validated": True})
    result = check_staging_validation(tc)
    assert "VALIDATED" in result
    assert "PASSED" in result


def test_check_staging_validation_blocked_when_false():
    tc = MockToolContext({"staging_validated": False})
    result = check_staging_validation(tc)
    assert "BLOCKED" in result


def test_check_staging_validation_blocked_when_missing():
    tc = MockToolContext({})
    result = check_staging_validation(tc)
    assert "BLOCKED" in result


# ===========================================================================
# 5. apply_knobs_production Tool Tests
# ===========================================================================

def test_apply_knobs_production_guardrail_blocks_unvalidated(mock_db_config_pg):
    tc = MockToolContext({
        "staging_validated": False,
        "prod_db_config": mock_db_config_pg,
        "selected_knobs": [{"knob": "work_mem", "recommended_value": "32MB", "restart_required": False}],
    })
    result = apply_knobs_production(tc)
    assert "ERROR: Guardrail check failed" in result


@patch("src.knob_tuner.sub_agents.live_tuner.tools.verify_active_knobs")
@patch("src.knob_tuner.sub_agents.live_tuner.tools.apply_knobs")
def test_apply_knobs_production_success_splits_dynamic_and_static(mock_apply, mock_verify, mock_db_config_pg):
    mock_apply.return_value = [
        {"knob": "work_mem", "value": "32MB", "status": "applied", "error": None},
        {"knob": "shared_buffers", "value": "4GB", "status": "applied", "error": None},
    ]
    mock_verify.return_value = {
        "status": "ok",
        "all_verified": True,
        "knobs": [
            {"knob": "work_mem", "expected_value": "32MB", "actual_value": "32MB", "status": "VERIFIED"},
            {"knob": "shared_buffers", "expected_value": "4GB", "actual_value": "4GB", "status": "PENDING_RESTART"},
        ],
    }
    knobs = [
        {"knob": "work_mem", "recommended_value": "32MB", "restart_required": False},
        {"knob": "shared_buffers", "recommended_value": "4GB", "restart_required": True, "reasoning": "25% RAM"},
    ]
    tc = MockToolContext({
        "staging_validated": True,
        "prod_db_config": mock_db_config_pg,
        "selected_knobs": knobs,
    })

    result = apply_knobs_production(tc)

    assert "Production Live Tuning Report" in result
    assert "**Knobs Processed**: 2/2" in result
    assert "**Static / Restart-Required Knobs Deferred**: 1" in result
    assert "**Auto-Restart Status**: DISABLED" in result
    assert "shared_buffers" in result
    assert "work_mem" in result
    assert tc.state["prod_restart_required_knobs"][0]["name"] == "shared_buffers"
    assert len(tc.state["prod_applied_knobs"]) == 2
    mock_apply.assert_called_once()


@patch("src.knob_tuner.sub_agents.live_tuner.tools.verify_active_knobs")
@patch("src.knob_tuner.sub_agents.live_tuner.tools.apply_knobs")
def test_apply_knobs_production_all_static_knobs(mock_apply, mock_verify, mock_db_config_pg):
    mock_apply.return_value = [
        {"knob": "shared_buffers", "value": "4GB", "status": "applied", "error": None},
        {"knob": "max_connections", "value": "500", "status": "applied", "error": None},
    ]
    mock_verify.return_value = {
        "status": "ok",
        "all_verified": False,
        "knobs": [
            {"knob": "shared_buffers", "expected_value": "4GB", "actual_value": "128MB", "status": "PENDING_RESTART"},
            {"knob": "max_connections", "expected_value": "500", "actual_value": "100", "status": "PENDING_RESTART"},
        ],
    }
    knobs = [
        {"knob": "shared_buffers", "recommended_value": "4GB", "restart_required": True},
        {"knob": "max_connections", "recommended_value": "500", "restart_required": True},
    ]
    tc = MockToolContext({
        "staging_validated": True,
        "prod_db_config": mock_db_config_pg,
        "selected_knobs": knobs,
    })

    result = apply_knobs_production(tc)

    assert "**Knobs Processed**: 2/2" in result
    assert "**Static / Restart-Required Knobs Deferred**: 2" in result
    mock_apply.assert_called_once()


def test_apply_knobs_production_missing_config():
    tc = MockToolContext({
        "staging_validated": True,
        "selected_knobs": [{"knob": "work_mem", "recommended_value": "32MB"}],
    })
    result = apply_knobs_production(tc)
    assert "ERROR: Production DBConfig not found in state" in result


def test_apply_knobs_production_no_knobs(mock_db_config_pg):
    tc = MockToolContext({
        "staging_validated": True,
        "prod_db_config": mock_db_config_pg,
    })
    result = apply_knobs_production(tc)
    assert "ERROR: No knob recommendations found" in result
