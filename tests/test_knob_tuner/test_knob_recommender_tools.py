"""Unit tests for knob_recommender sub-agent models, agent creation, and tools."""

import json
import os
import tempfile
from pathlib import Path
import pytest

from src.knob_tuner.sub_agents.knob_recommender.agent import (
    create_knob_recommender_agent,
)
from src.knob_tuner.sub_agents.knob_recommender.models import (
    KnobRecommendation,
    KnobRecommenderOutput,
)
from src.knob_tuner.sub_agents.knob_recommender.tools import (
    read_knobs_file,
    write_selected_knobs,
)


class MockToolContext:
    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}


# ===========================================================================
# 1. Pydantic Models Validation Tests
# ===========================================================================

def test_knob_recommendation_model_validates():
    rec = KnobRecommendation(
        knob="shared_buffers",
        current_value="128MB",
        recommended_value="4GB",
        unit="GB",
        reasoning="25% of 16GB total RAM for dedicated PostgreSQL OLTP instance",
        restart_required=True,
        risk_level="medium",
    )
    assert rec.knob == "shared_buffers"
    assert rec.current_value == "128MB"
    assert rec.recommended_value == "4GB"
    assert rec.restart_required is True
    assert rec.risk_level == "medium"
    dump = rec.model_dump()
    assert dump["knob"] == "shared_buffers"
    assert KnobRecommendation.model_validate(dump).recommended_value == "4GB"


def test_knob_recommendation_defaults():
    rec = KnobRecommendation(
        knob="work_mem",
        current_value="4MB",
        recommended_value="32MB",
        reasoning="Sufficient workspace for sort operations with 100 max connections",
    )
    assert rec.unit == ""
    assert rec.restart_required is False
    assert rec.risk_level == "low"


def test_knob_recommender_output_validates():
    data = {
        "total_memory_allocated_gb": 12.0,
        "memory_budget_pct": 75.0,
        "recommendations": [
            {
                "knob": "innodb_buffer_pool_size",
                "current_value": "134217728",
                "recommended_value": "10737418240",
                "unit": "Bytes",
                "reasoning": "Allocated 10GB (62.5% of 16GB RAM) to InnoDB buffer pool",
                "restart_required": False,
                "risk_level": "low",
            },
            {
                "knob": "max_connections",
                "current_value": "151",
                "recommended_value": "300",
                "unit": "",
                "reasoning": "Accommodate connection pool peak spikes",
                "restart_required": False,
                "risk_level": "low",
            },
        ],
        "summary": "Optimized memory buffers for read-heavy OLTP workload on 16GB host.",
        "restart_required": False,
    }
    out = KnobRecommenderOutput.model_validate(data)
    assert out.total_memory_allocated_gb == 12.0
    assert out.memory_budget_pct == 75.0
    assert len(out.recommendations) == 2
    assert out.recommendations[0].knob == "innodb_buffer_pool_size"
    assert out.restart_required is False


# ===========================================================================
# 2. Agent Factory Test
# ===========================================================================

def test_create_knob_recommender_agent():
    agent = create_knob_recommender_agent()
    assert agent.name == "knob_recommender"
    assert agent.output_key == "knob_recommender_output"
    assert agent.output_schema == KnobRecommenderOutput
    assert len(agent.tools) == 3
    tool_names = [t.__name__ for t in agent.tools]
    assert "read_knobs_file" in tool_names
    assert "write_selected_knobs" in tool_names
    assert "get_knob_strategies" in tool_names


# ===========================================================================
# 3. read_knobs_file Tool Tests
# ===========================================================================

def test_read_knobs_file_from_disk_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        knobs_data = [
            {
                "name": "shared_buffers",
                "current_value": "128MB",
                "unit": "MB",
                "category": "Resource Usage / Memory",
                "description": "Sets memory for shared buffers",
            },
            {
                "name": "work_mem",
                "current_value": "4MB",
                "unit": "MB",
                "category": "Resource Usage / Memory",
                "description": "Sets memory for query workspaces",
            },
        ]
        knobs_path = os.path.join(tmpdir, "knobs.json")
        with open(knobs_path, "w", encoding="utf-8") as f:
            json.dump(knobs_data, f)

        tc = MockToolContext({"knob_path": tmpdir})
        result = read_knobs_file(tc)

        assert "Read 2 tunable knobs" in result
        assert "shared_buffers" in result
        assert "work_mem" in result
        assert "knobs_info" in tc.state
        assert len(tc.state["knobs_info"]) == 2


def test_read_knobs_file_from_disk_dict():
    with tempfile.TemporaryDirectory() as tmpdir:
        knobs_data = {
            "available_knobs": [
                {
                    "name": "innodb_buffer_pool_size",
                    "current_value": "134217728",
                    "unit": "",
                    "category": "InnoDB",
                }
            ]
        }
        knobs_path = os.path.join(tmpdir, "knobs.json")
        with open(knobs_path, "w", encoding="utf-8") as f:
            json.dump(knobs_data, f)

        tc = MockToolContext({"target": tmpdir})
        result = read_knobs_file(tc)

        assert "Read 1 tunable knobs" in result
        assert "innodb_buffer_pool_size" in result
        assert len(tc.state["knobs_info"]) == 1


def test_read_knobs_file_fallback_to_state():
    knobs_state = [
        {"name": "max_wal_size", "current_value": "1GB", "category": "WAL"}
    ]
    tc = MockToolContext({"knob_path": "/nonexistent/dir", "knobs_info": knobs_state})
    result = read_knobs_file(tc)

    assert "Read 1 tunable knobs" in result
    assert "max_wal_size" in result


def test_read_knobs_file_missing_everywhere():
    tc = MockToolContext({"knob_path": "/nonexistent/dir"})
    result = read_knobs_file(tc)
    assert "ERROR: knobs file not found" in result


# ===========================================================================
# 4. write_selected_knobs Tool Tests
# ===========================================================================

def test_write_selected_knobs_from_model_output():
    with tempfile.TemporaryDirectory() as tmpdir:
        rec1 = KnobRecommendation(
            knob="shared_buffers",
            current_value="128MB",
            recommended_value="4GB",
            unit="GB",
            reasoning="25% RAM",
            restart_required=True,
        )
        rec2 = KnobRecommendation(
            knob="work_mem",
            current_value="4MB",
            recommended_value="32MB",
            unit="MB",
            reasoning="Sort workspace",
            restart_required=False,
        )
        output = KnobRecommenderOutput(
            total_memory_allocated_gb=4.5,
            memory_budget_pct=56.25,
            recommendations=[rec1, rec2],
            summary="Postgres tuning",
            restart_required=True,
        )

        tc = MockToolContext({
            "knob_path": tmpdir,
            "knob_recommender_output": output,
        })
        result = write_selected_knobs(tc)

        assert "OK: wrote 2 selected knobs" in result
        out_file = os.path.join(tmpdir, "knobs-selected.json")
        assert os.path.isfile(out_file)

        with open(out_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 2
        assert data[0]["knob"] == "shared_buffers"
        assert data[0]["recommended_value"] == "4GB"
        assert data[0]["restart_required"] is True
        assert tc.state["selected_knobs"] == data


def test_write_selected_knobs_from_dict_output():
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dict = {
            "recommendations": [
                {
                    "knob": "innodb_buffer_pool_size",
                    "current_value": "128M",
                    "recommended_value": "8G",
                    "reasoning": "60% RAM",
                    "restart_required": False,
                }
            ]
        }
        tc = MockToolContext({
            "target": tmpdir,
            "knob_recommender_output": output_dict,
        })
        result = write_selected_knobs(tc)

        assert "OK: wrote 1 selected knobs" in result
        out_file = os.path.join(tmpdir, "knobs-selected.json")
        assert os.path.isfile(out_file)


def test_write_selected_knobs_from_selected_knobs_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        selected_list = [
            {"knob": "random_page_cost", "current_value": "4.0", "recommended_value": "1.1", "reasoning": "SSD"}
        ]
        tc = MockToolContext({
            "knob_path": tmpdir,
            "selected_knobs": selected_list,
        })
        result = write_selected_knobs(tc)

        assert "OK: wrote 1 selected knobs" in result
        out_file = os.path.join(tmpdir, "knobs-selected.json")
        assert os.path.isfile(out_file)


def test_write_selected_knobs_missing_recommendations():
    tc = MockToolContext({"knob_path": "/tmp"})
    result = write_selected_knobs(tc)
    assert "ERROR: no selected/recommended knobs found in state" in result
