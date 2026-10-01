"""Unit tests for the read-only knob detail tool.

Relocated from the retired ``knob_recommender`` package: ``read_knob_details``
now lives in ``src.knob_tuner.tools.reading`` and the retired
``KnobRecommendation`` model tests were removed with it. Pure-agent
coverage (candidate_generator tools == {get_knob_strategies,
read_knob_details}, no write_* strings in prompts) lives in
test_pure_agents.py and is not duplicated here. ``read_knob_details`` is
kept because the candidate_generator re-exports and uses it.
"""

import json
import os
import tempfile

from src.knob_tuner.tools.reading import (
    read_knob_details,
)


class MockToolContext:
    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}


# ===========================================================================
# 1. Pure-agent prompt guardrails (candidate_generator)
# ===========================================================================
# Tool-list purity (tools == {get_knob_strategies, read_knob_details}) and
# the no-write_* prompt invariant are asserted in test_pure_agents.py and
# are not repeated here. This test pins the tuning guardrails the pure
# agent still carries.

def test_candidate_generator_prompt_guardrails():
    from src.knob_tuner.sub_agents.candidate_generator.prompt import (
        CANDIDATE_GENERATOR_PROMPT,
    )
    assert "max_parallel_workers" in CANDIDATE_GENERATOR_PROMPT
    assert "max_parallel_workers_per_gather" in CANDIDATE_GENERATOR_PROMPT
    assert "max_worker_processes" in CANDIDATE_GENERATOR_PROMPT
    assert "effective_cache_size" in CANDIDATE_GENERATOR_PROMPT
    assert "autovacuum_vacuum_scale_factor >= 0.10" in CANDIDATE_GENERATOR_PROMPT
    assert "autovacuum_vacuum_cost_limit <= 400" in CANDIDATE_GENERATOR_PROMPT
    assert "wal_buffers" in CANDIDATE_GENERATOR_PROMPT
    assert "max_wal_size >= 4GB" in CANDIDATE_GENERATOR_PROMPT
    assert "checkpoint_completion_target = 0.9" in CANDIDATE_GENERATOR_PROMPT
    # names-first selection + inventory grounding
    assert "available-knob list" in CANDIDATE_GENERATOR_PROMPT
    assert "read_knob_details" in CANDIDATE_GENERATOR_PROMPT
    assert "Never" in CANDIDATE_GENERATOR_PROMPT
    # durability policy is explicit — Phase 4.4: strict-always, no relaxed
    # mode (the prompt states the strict rule and forbids relaxing it).
    assert "strict" in CANDIDATE_GENERATOR_PROMPT
    assert "no relaxed mode" in CANDIDATE_GENERATOR_PROMPT
    assert "synchronous_commit" in CANDIDATE_GENERATOR_PROMPT



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
