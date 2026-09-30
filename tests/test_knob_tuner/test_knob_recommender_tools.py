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
    read_knob_details,
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
    assert agent.generate_content_config.temperature == 0.0
    assert len(agent.tools) == 3
    tool_names = [t.__name__ for t in agent.tools]
    assert "read_knob_details" in tool_names
    assert "write_selected_knobs" in tool_names
    assert "get_knob_strategies" in tool_names


def test_knob_recommender_prompt_guardrails():
    from src.knob_tuner.sub_agents.knob_recommender.prompt import KNOB_RECOMMENDER_PROMPT
    assert "max_parallel_workers" in KNOB_RECOMMENDER_PROMPT
    assert "max_parallel_workers_per_gather" in KNOB_RECOMMENDER_PROMPT
    assert "max_worker_processes" in KNOB_RECOMMENDER_PROMPT
    assert "effective_cache_size" in KNOB_RECOMMENDER_PROMPT
    assert "autovacuum_vacuum_scale_factor >= 0.10" in KNOB_RECOMMENDER_PROMPT
    assert "autovacuum_vacuum_cost_limit <= 400" in KNOB_RECOMMENDER_PROMPT
    assert "wal_buffers" in KNOB_RECOMMENDER_PROMPT
    assert "max_wal_size >= 4GB" in KNOB_RECOMMENDER_PROMPT
    assert "checkpoint_completion_target = 0.9" in KNOB_RECOMMENDER_PROMPT
    # names-first selection + inventory grounding
    assert "LIST OF AVAILABLE KNOB NAMES" in KNOB_RECOMMENDER_PROMPT
    assert "read_knob_details" in KNOB_RECOMMENDER_PROMPT
    assert "Never" in KNOB_RECOMMENDER_PROMPT
    # durability policy is explicit
    assert "strict" in KNOB_RECOMMENDER_PROMPT
    assert "relaxed" in KNOB_RECOMMENDER_PROMPT
    assert "synchronous_commit" in KNOB_RECOMMENDER_PROMPT



# ===========================================================================
# 3. read_knob_details Tool Tests
# ===========================================================================

def test_read_knob_details_from_disk_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        knobs_data = [
            {
                "name": "shared_buffers",
                "current_value": "128",
                "unit": "MB",
                "context": "user",
                "vartype": "integer",
                "min_val": 16,
                "max_val": 16384,
                "pending_restart": False,
                "description": "Sets memory for shared buffers",
            },
            {
                "name": "work_mem",
                "current_value": "4",
                "unit": "MB",
                "context": "user",
                "vartype": "integer",
                "pending_restart": False,
                "description": "Sets memory for query workspaces",
            },
            {
                "name": "random_page_cost",
                "current_value": "4",
                "unit": "",
                "context": "user",
                "description": "Cost of a non-sequential page fetch",
            },
        ]
        knobs_path = os.path.join(tmpdir, "knobs.json")
        with open(knobs_path, "w", encoding="utf-8") as f:
            json.dump(knobs_data, f)

        tc = MockToolContext({"knob_path": tmpdir})
        result = read_knob_details("shared_buffers, work_mem", tc)

        assert "shared_buffers" in result
        assert "work_mem" in result
        assert "128 MB" in result
        assert "4 MB" in result
        assert "context=user" in result
        assert "range=16..16384" in result
        assert "random_page_cost" not in result


def test_read_knob_details_fallback_to_state():
    knobs_state = [
        {
            "name": "max_wal_size",
            "current_value": "1",
            "unit": "GB",
            "context": "sighup",
            "description": "Maximum size to let the WAL grow",
        }
    ]
    tc = MockToolContext({"knob_path": "/nonexistent/dir", "knobs_info": knobs_state})
    result = read_knob_details("max_wal_size", tc)

    assert "max_wal_size" in result
    assert "1 GB" in result
    assert "context=sighup" in result


def test_read_knob_details_missing_name():
    knobs_state = [
        {"name": "shared_buffers", "current_value": "128", "unit": "MB"}
    ]
    tc = MockToolContext(
        {"knob_path": "/nonexistent/dir", "knobs_info": knobs_state}
    )
    result = read_knob_details("shared_buffers, bogus_knob", tc)

    assert "shared_buffers" in result
    assert "128 MB" in result
    assert "Missing: bogus_knob" in result


def test_read_knob_details_missing_everywhere():
    tc = MockToolContext({"knob_path": "/nonexistent/dir"})
    result = read_knob_details("shared_buffers", tc)
    assert result.startswith("ERROR:")


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
            "memory_gb": 16.0,
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
