"""Tests for the deterministic knob_tuner workflow, CLI parsing, session init, and runner."""

import asyncio
import inspect
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk import Event, Workflow
from google.adk.agents import LlmAgent

from src.knob_tuner.agent import create_root_agent
from src.knob_tuner.workflow import create_knob_tuner_workflow
from src.knob_tuner.contracts import ResourceBudget, SysbenchProfile
from src.knob_tuner.main import (
    DEFAULT_MODEL,
    _derive_status,
    _load_profile,
    _log_event,
    _maybe_parse,
    _parse_budget,
    _process_cleanup,
    _signal_handler,
    _write_output_result,
    build_initial_state,
    build_parser,
    main,
    register_cleanup_handlers,
    run_pipeline,
)
from src.knob_tuner.tools.db_connector import DBConfig
from src.knob_tuner.tools.validation import SnapshotRegistry
from src.knob_tuner.workflow import (
    _recommendation_instruction,
    _resolve_db_config,
    apply_live_node,
    finalize_node,
    make_tune_loop,
    validate_budget_node,
)
from src.knob_tuner.tools.docker_tools import (
    ACTIVE_CONTAINERS,
    register_active_container,
    unregister_active_container,
)


# ===========================================================================
# Helpers
# ===========================================================================


class _FakeContext:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state if state is not None else {}

    async def run_node(self, node, node_input=None):
        result = node(self, node_input)
        if inspect.isawaitable(result):
            result = await result
        return result


async def _drive(gen):
    return [event async for event in gen]


# ===========================================================================
# 1. Workflow initialization
# ===========================================================================


def test_create_root_agent_returns_workflow():
    agent = create_root_agent()
    assert isinstance(agent, Workflow)
    assert not isinstance(agent, LlmAgent)
    assert agent.name == "adco_knob_tuner"

    names = {node.name for node in agent.graph.nodes}
    assert {
        "validate_budget_node",
        "db_inspector",
        "tune_loop",
        "apply_live_node",
        "finalize_node",
    } <= names


def test_workflow_edge_order():
    agent = create_root_agent()
    edges = [(e.from_node.name, e.to_node.name) for e in agent.graph.edges]
    assert edges == [
        ("__START__", "validate_budget_node"),
        ("validate_budget_node", "db_inspector"),
        ("db_inspector", "tune_loop"),
        ("tune_loop", "apply_live_node"),
        ("apply_live_node", "finalize_node"),
    ]


def test_workflow_forwards_model():
    workflow = create_knob_tuner_workflow(model="gemini-1.5-pro")
    inspector = next(n for n in workflow.graph.nodes if n.name == "db_inspector")
    assert isinstance(inspector, LlmAgent)
    assert inspector.model == "gemini-1.5-pro"


def test_workflow_has_no_orchestrator_llm():
    agent = create_root_agent()
    assert isinstance(agent, Workflow)
    assert type(agent).__name__ == "Workflow"


# ===========================================================================
# 2. validate_budget_node
# ===========================================================================


def test_validate_budget_node_missing_raises():
    ctx = _FakeContext({})
    with pytest.raises(ValueError, match="resource_budget is required"):
        validate_budget_node(ctx)


def test_validate_budget_node_invalid_raises():
    ctx = _FakeContext({"resource_budget": {"cpu_cores": 0, "memory_gb": 1}})
    with pytest.raises(ValueError, match="invalid resource_budget"):
        validate_budget_node(ctx)


def test_validate_budget_node_valid():
    ctx = _FakeContext({"resource_budget": {"cpu_cores": 4, "memory_gb": 8.0}})
    event = validate_budget_node(ctx)
    assert isinstance(event, Event)
    assert event.output == {"cpu_cores": 4, "memory_gb": 8.0}
    assert event.actions.state_delta["resource_budget"] == {
        "cpu_cores": 4,
        "memory_gb": 8.0,
    }


# ===========================================================================
# 3. make_tune_loop (Python-owned retry loop)
# ===========================================================================


def _recommender(state_update):
    def node(ctx, node_input=None):
        ctx.state["knob_recommender_output"] = state_update

    return node


def _inventory() -> list[dict]:
    def knob(name, context, vartype="integer", enumvals=None, current="1"):
        return {
            "name": name,
            "current_value": current,
            "unit": "",
            "category": "Test",
            "description": "",
            "min_val": "",
            "max_val": "",
            "context": context,
            "vartype": vartype,
            "enumvals": enumvals or [],
            "pending_restart": False,
        }

    return [
        knob("shared_buffers", "postmaster", current="128MB"),
        knob("work_mem", "user", current="4MB"),
        knob("effective_cache_size", "user", current="4GB"),
        knob("max_worker_processes", "postmaster"),
        knob("autovacuum_vacuum_scale_factor", "sighup", vartype="real", current="0.2"),
        knob(
            "synchronous_commit",
            "user",
            vartype="enum",
            enumvals=["on", "off", "local", "remote_write", "remote_apply"],
            current="on",
        ),
        knob("full_page_writes", "postmaster", vartype="bool", current="on"),
    ]


def _base_loop_state(**overrides):
    inventory = _inventory()
    state = {
        "resource_budget": {"cpu_cores": 2, "memory_gb": 4.0},
        "sysbench_profile": {},
        "apply_mode": "dynamic",
        "dry_run": False,
        "run_id": "run-1",
        "run_dir": "/tmp/run-1",
        "db_type": "postgres",
        "database": "testdb",
        "max_validation_attempts": 4,
        "durability_profile": "strict",
        "knobs_info": inventory,
        "available_knob_names": [k["name"] for k in inventory],
    }
    state.update(overrides)
    return state


def _paired(baseline, tuned):
    return {
        "status": "PASS",
        "baseline": {"per_run_tps": list(baseline)},
        "tuned": {"per_run_tps": list(tuned)},
    }


def test_recommendation_instruction_includes_workload_hint():
    instruction = _recommendation_instruction(
        1, [], {}, ["work_mem", "shared_buffers"], "strict", "Analytical GROUP BY spills"
    )
    assert "Analytical GROUP BY spills" in instruction
    assert "work_mem" in instruction


def test_recommendation_instruction_omits_empty_hint():
    instruction = _recommendation_instruction(
        1, [], {}, ["work_mem"], "strict", ""
    )
    assert "workload context" not in instruction.lower()


def test_tune_loop_threads_screening_benchmark_to_validator():
    calls = []

    def validator(**kwargs):
        calls.append(kwargs)
        return {
            "status": "PASS",
            "attestation": {"run_id": "run-1"},
            "paired": _paired([1.0, 1.0, 1.0], [8.0, 8.0, 8.0]),
            "reasons": [],
        }

    recommender = _recommender(
        {"recommendations": [{"knob": "work_mem", "recommended_value": "256MB"}]}
    )
    ctx = _FakeContext(_base_loop_state(screening_benchmark="pgbench"))
    asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    assert calls
    assert all(call["benchmark_kind"] == "pgbench" for call in calls)


def test_tune_loop_defaults_screening_benchmark_to_sysbench():
    calls = []

    def validator(**kwargs):
        calls.append(kwargs)
        return {
            "status": "PASS",
            "attestation": {"run_id": "run-1"},
            "paired": _paired([100.0, 100.0, 100.0], [105.0, 105.0, 105.0]),
            "reasons": [],
        }

    recommender = _recommender(
        {"recommendations": [{"knob": "work_mem", "recommended_value": "64MB"}]}
    )
    ctx = _FakeContext(_base_loop_state())
    asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    assert calls
    assert all(call["benchmark_kind"] == "sysbench" for call in calls)


def test_tune_loop_passes_first_attempt():
    calls = []

    def validator(**kwargs):
        calls.append(kwargs)
        return {
            "status": "PASS",
            "attestation": {"run_id": "run-1"},
            "paired": _paired([100.0, 100.0, 100.0], [105.0, 105.0, 105.0]),
            "reasons": [],
        }

    recommender = _recommender(
        {"recommendations": [{"knob": "shared_buffers", "recommended_value": "1GB"}]}
    )
    ctx = _FakeContext(_base_loop_state())
    events = asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    delta = events[-1].actions.state_delta
    # Single LLM candidate: screened once, then confirmed with a fresh,
    # independent measurement (never the screening result itself).
    assert len(calls) == 2
    assert delta["result_status"] == "PASS"
    assert delta["staging_validated"] is True
    assert delta["validation_attempts"][0]["candidate"] == "llm"
    assert delta["knob_plan"]["knobs"][0]["name"] == "shared_buffers"
    assert "multi_fidelity" in delta["candidate_archive"]
    confirm_entries = [
        entry
        for entry in delta["validation_attempts"]
        if entry.get("phase") == "confirm"
    ]
    assert confirm_entries and confirm_entries[0]["confirmed"] is True


def test_tune_loop_no_valid_plan_is_inconclusive():
    calls = []

    def validator(**kwargs):
        calls.append(kwargs)
        return {"status": "PASS", "paired": _paired([100.0], [100.0]), "reasons": []}

    ctx = _FakeContext(_base_loop_state(max_validation_attempts=2))
    events = asyncio.run(
        _drive(make_tune_loop(_recommender({"recommendations": []}), validator)(ctx))
    )

    delta = events[-1].actions.state_delta
    assert calls == []
    assert delta["result_status"] == "INCONCLUSIVE"
    assert delta["staging_validated"] is False
    assert delta["knob_plan"]["knobs"] == []


def test_tune_loop_environment_failure_is_fail():
    calls = []

    def validator(**kwargs):
        calls.append(kwargs)
        return {"status": "FAIL", "paired": None, "reasons": ["env error"]}

    recommender = _recommender(
        {"recommendations": [{"knob": "shared_buffers", "recommended_value": "1GB"}]}
    )
    ctx = _FakeContext(_base_loop_state())
    events = asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    assert len(calls) == 1
    assert events[-1].actions.state_delta["result_status"] == "FAIL"


def test_tune_loop_rejects_likely_regression():
    def validator(**kwargs):
        # Clear regression: upper bound well below zero → not eligible to confirm.
        return {
            "status": "FAIL",
            "paired": _paired([100.0, 100.0, 100.0], [80.0, 85.0, 82.0]),
            "reasons": [],
        }

    recommender = _recommender(
        {"recommendations": [{"knob": "work_mem", "recommended_value": "4MB"}]}
    )
    ctx = _FakeContext(_base_loop_state())
    events = asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    delta = events[-1].actions.state_delta
    assert delta["result_status"] == "INCONCLUSIVE"
    assert delta["knob_plan"]["knobs"] == []


def test_tune_loop_negative_lcb_is_not_promoted():
    def validator(**kwargs):
        # Noisy improvement: positive mean, negative LCB; health check passes.
        return {
            "status": "PASS",
            "paired": _paired([100.0, 100.0, 100.0], [106.0, 95.0, 110.0]),
            "reasons": [],
        }

    recommender = _recommender(
        {"recommendations": [{"knob": "work_mem", "recommended_value": "4MB"}]}
    )
    ctx = _FakeContext(_base_loop_state())
    events = asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    delta = events[-1].actions.state_delta
    # A paired PASS alone is not enough: the 95% LCB is negative, so the
    # candidate is not promoted.
    assert delta["result_status"] == "INCONCLUSIVE"
    assert delta["staging_validated"] is False
    assert delta["improvement_confident"] is False


def test_tune_loop_lcb_below_minimum_is_not_promoted():
    def validator(**kwargs):
        # +1% with zero spread: positive LCB, but below the 2% default minimum.
        return {
            "status": "PASS",
            "paired": _paired([100.0, 100.0, 100.0], [101.0, 101.0, 101.0]),
            "reasons": [],
        }

    recommender = _recommender(
        {"recommendations": [{"knob": "work_mem", "recommended_value": "4MB"}]}
    )
    ctx = _FakeContext(_base_loop_state())
    events = asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    delta = events[-1].actions.state_delta
    assert delta["result_status"] == "INCONCLUSIVE"
    assert delta["staging_validated"] is False


def test_tune_loop_lcb_above_minimum_is_promoted():
    def validator(**kwargs):
        # +3% with zero spread: LCB clears the default 2% minimum.
        return {
            "status": "PASS",
            "paired": _paired([100.0, 100.0, 100.0], [103.0, 103.0, 103.0]),
            "reasons": [],
        }

    recommender = _recommender(
        {"recommendations": [{"knob": "work_mem", "recommended_value": "256MB"}]}
    )
    ctx = _FakeContext(_base_loop_state())
    events = asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    delta = events[-1].actions.state_delta
    assert delta["result_status"] == "PASS"
    assert delta["staging_validated"] is True
    assert delta["improvement_confident"] is True
    assert delta["knob_plan"]["knobs"][0]["name"] == "work_mem"


def test_tune_loop_rejects_unknown_knob():
    calls = []

    def validator(**kwargs):
        calls.append(kwargs)
        return {"status": "PASS", "paired": _paired([100.0], [100.0]), "reasons": []}

    recommender = _recommender(
        {"recommendations": [{"knob": "made_up_knob", "recommended_value": "1"}]}
    )
    ctx = _FakeContext(_base_loop_state(max_validation_attempts=2))
    events = asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    assert calls == []
    delta = events[-1].actions.state_delta
    assert delta["result_status"] == "INCONCLUSIVE"
    assert any("not in available knob inventory" in r for r in delta["staging_issues"])


def test_tune_loop_rejects_noop_recommendation():
    calls = []

    def validator(**kwargs):
        calls.append(kwargs)
        return {"status": "PASS", "paired": _paired([100.0], [100.0]), "reasons": []}

    # work_mem's inventory current value is 4MB, so this changes nothing and must
    # not be promoted as if it did.
    recommender = _recommender(
        {"recommendations": [{"knob": "work_mem", "recommended_value": "4MB"}]}
    )
    ctx = _FakeContext(_base_loop_state(max_validation_attempts=2))
    events = asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    assert calls == []
    delta = events[-1].actions.state_delta
    assert delta["result_status"] == "INCONCLUSIVE"
    assert any("no-op" in r for r in delta["staging_issues"])


def test_tune_loop_durability_policy_gates_synchronous_commit():
    def validator(**kwargs):
        return {
            "status": "PASS",
            "paired": _paired([100.0, 100.0, 100.0], [105.0, 104.0, 106.0]),
            "reasons": [],
        }

    rec = {"recommendations": [{"knob": "synchronous_commit", "recommended_value": "off"}]}

    strict_ctx = _FakeContext(_base_loop_state(max_validation_attempts=2))
    strict_events = asyncio.run(
        _drive(make_tune_loop(_recommender(rec), validator)(strict_ctx))
    )
    strict_delta = strict_events[-1].actions.state_delta
    assert strict_delta["result_status"] == "INCONCLUSIVE"
    assert strict_delta["knob_plan"]["knobs"] == []

    relaxed_ctx = _FakeContext(
        _base_loop_state(durability_profile="relaxed")
    )
    relaxed_events = asyncio.run(
        _drive(make_tune_loop(_recommender(rec), validator)(relaxed_ctx))
    )
    relaxed_delta = relaxed_events[-1].actions.state_delta
    assert relaxed_delta["result_status"] == "PASS"
    assert relaxed_delta["knob_plan"]["knobs"][0]["name"] == "synchronous_commit"


def _pass_validator(captured=None):
    def validator(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        return {
            "status": "PASS",
            "paired": _paired([100.0, 100.0, 100.0], [105.0, 105.0, 105.0]),
            "reasons": [],
        }

    return validator


def test_tune_loop_derives_realistic_dataset_from_schema_info():
    captured = []
    ctx = _FakeContext(
        _base_loop_state(
            schema_info=[
                {"approximate_row_count": 600000},
                {"approximate_row_count": 400000},
            ]
        )
    )
    events = asyncio.run(
        _drive(
            make_tune_loop(
                _recommender({"recommendations": [{"knob": "work_mem", "recommended_value": "256MB"}]}),
                _pass_validator(captured),
            )(ctx)
        )
    )

    delta = events[-1].actions.state_delta
    dataset = delta["screen_dataset"]
    assert dataset["source"] == "target_rows"
    assert dataset["total_rows"] == 1_000_000
    assert dataset["tables"] == 10
    assert dataset["rows_per_table"] == 100_000
    # The derived profile is what the validator actually measures.
    assert captured[0]["profile"].rows_per_table == 100_000


def test_tune_loop_dataset_falls_back_without_schema_info():
    ctx = _FakeContext(_base_loop_state())
    events = asyncio.run(
        _drive(
            make_tune_loop(
                _recommender({"recommendations": [{"knob": "work_mem", "recommended_value": "4MB"}]}),
                _pass_validator(),
            )(ctx)
        )
    )
    assert events[-1].actions.state_delta["screen_dataset"]["source"] == "default"


def test_tune_loop_screen_total_rows_override():
    captured = []
    ctx = _FakeContext(
        _base_loop_state(
            schema_info=[{"approximate_row_count": 600000}],
            screen_total_rows=200000,
        )
    )
    events = asyncio.run(
        _drive(
            make_tune_loop(
                _recommender({"recommendations": [{"knob": "work_mem", "recommended_value": "4MB"}]}),
                _pass_validator(captured),
            )(ctx)
        )
    )
    dataset = events[-1].actions.state_delta["screen_dataset"]
    assert dataset["source"] == "explicit"
    assert dataset["total_rows"] == 200000


def test_tune_loop_preserves_restart_required_for_unknown_scope():
    captured = {}

    def validator(**kwargs):
        captured["plan"] = kwargs["plan"]
        return {"status": "PASS", "paired": {"delta_pct": 1.0}, "reasons": []}

    recommender = _recommender(
        {
            "recommendations": [
                {
                    "knob": "shared_buffers",
                    "recommended_value": "1GB",
                    "restart_required": True,
                }
            ]
        }
    )
    ctx = _FakeContext(_base_loop_state())
    asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    spec = captured["plan"].knobs[0]
    assert spec.restart_required is True


def test_tune_loop_dry_run_calls_validator_with_dry_run():
    captured = {}

    def validator(**kwargs):
        captured.update(kwargs)
        return {"status": "INCONCLUSIVE", "paired": None, "reasons": ["dry-run"]}

    recommender = _recommender(
        {"recommendations": [{"knob": "shared_buffers", "recommended_value": "1GB"}]}
    )
    ctx = _FakeContext(_base_loop_state(dry_run=True, max_validation_attempts=1))
    events = asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    assert captured["dry_run"] is True
    assert events[-1].actions.state_delta["staging_validated"] is False


def test_tune_loop_emits_progress_to_log_file(tmp_path: Path):
    log_file = tmp_path / "progress.log"

    def validator(**kwargs):
        return {"status": "PASS", "paired": {"delta_pct": 1.0}, "reasons": []}

    recommender = _recommender(
        {"recommendations": [{"knob": "shared_buffers", "recommended_value": "1GB"}]}
    )
    ctx = _FakeContext(
        _base_loop_state(verbose=True, log_file=str(log_file))
    )
    asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    content = log_file.read_text(encoding="utf-8")
    assert "Attempt 1/" in content
    assert "Recommendation received:" in content


# ===========================================================================
# 4. apply_live_node / finalize_node
# ===========================================================================


def _plan_dump():
    return {
        "knobs": [
            {
                "name": "shared_buffers",
                "value": "1GB",
                "scope": "user",
                "restart_required": False,
                "reasoning": "",
            }
        ]
    }


def test_apply_live_node_dry_run_skips_without_mutation():
    ctx = _FakeContext(
        {"dry_run": True, "result_status": "PASS", "knob_plan": _plan_dump()}
    )
    with patch("src.knob_tuner.workflow.apply_knobs") as mock_apply:
        event = apply_live_node(ctx)
        mock_apply.assert_not_called()
    assert event.output["status"] == "SKIPPED"


def test_apply_live_node_non_pass_skips():
    ctx = _FakeContext(
        {"dry_run": False, "result_status": "FAIL", "knob_plan": _plan_dump()}
    )
    with patch("src.knob_tuner.workflow.apply_knobs") as mock_apply:
        event = apply_live_node(ctx)
        mock_apply.assert_not_called()
    assert event.output["status"] == "SKIPPED"


def test_apply_live_node_applies_on_pass():
    cfg = DBConfig(
        host="127.0.0.1",
        port=5432,
        user="u",
        password="p",
        database="d",
        db_type="postgres",
    )
    ctx = _FakeContext(
        {
            "dry_run": False,
            "result_status": "PASS",
            "knob_plan": _plan_dump(),
            "db_config": cfg,
            "apply_mode": "dynamic",
        }
    )
    applied = [{"knob": "shared_buffers", "value": "1GB", "status": "applied"}]
    with patch("src.knob_tuner.workflow.apply_knobs", return_value=applied) as mock_apply:
        event = apply_live_node(ctx)
        mock_apply.assert_called_once()
    assert event.output["status"] == "APPLIED"
    assert event.actions.state_delta["applied_knobs"] == applied


def test_apply_live_node_maintenance_assisted_emits_manual_sql():
    cfg = DBConfig(
        host="127.0.0.1",
        port=5432,
        user="u",
        password="p",
        database="d",
        db_type="postgres",
    )
    ctx = _FakeContext(
        {
            "dry_run": False,
            "result_status": "PASS",
            "knob_plan": _plan_dump(),
            "db_config": cfg,
            "apply_mode": "maintenance-assisted",
        }
    )
    sql_results = [
        {
            "knob": "shared_buffers",
            "value": "1GB",
            "status": "dry_run",
            "sql": "ALTER SYSTEM SET shared_buffers = '1GB';",
            "error": None,
        }
    ]
    with patch(
        "src.knob_tuner.workflow.apply_knobs", return_value=sql_results
    ) as mock_apply:
        event = apply_live_node(ctx)

    assert event.output["status"] == "MANUAL_SQL"
    assert event.output["manual_sql"] == ["ALTER SYSTEM SET shared_buffers = '1GB';"]
    assert event.actions.state_delta["applied_knobs"] == []
    # Manual SQL is not an applied success: the run must not stay PASS.
    assert event.actions.state_delta["result_status"] == "INCONCLUSIVE"
    # Production is never mutated: SQL is generated with dry_run only.
    assert mock_apply.call_args.kwargs.get("dry_run") is True


def test_apply_live_node_apply_failure_sets_result_status_fail():
    cfg = DBConfig(
        host="127.0.0.1",
        port=5432,
        user="u",
        password="p",
        database="d",
        db_type="postgres",
    )
    ctx = _FakeContext(
        {
            "dry_run": False,
            "result_status": "PASS",
            "knob_plan": _plan_dump(),
            "db_config": cfg,
            "apply_mode": "dynamic",
        }
    )
    with patch(
        "src.knob_tuner.workflow.apply_knobs", side_effect=RuntimeError("boom")
    ):
        event = apply_live_node(ctx)

    assert event.output["status"] == "FAILED"
    assert event.actions.state_delta["result_status"] == "FAIL"


def test_apply_live_node_maintenance_sql_failure_sets_result_status_fail():
    cfg = DBConfig(
        host="127.0.0.1",
        port=5432,
        user="u",
        password="p",
        database="d",
        db_type="postgres",
    )
    ctx = _FakeContext(
        {
            "dry_run": False,
            "result_status": "PASS",
            "knob_plan": _plan_dump(),
            "db_config": cfg,
            "apply_mode": "maintenance-assisted",
        }
    )
    with patch(
        "src.knob_tuner.workflow.apply_knobs", side_effect=RuntimeError("boom")
    ):
        event = apply_live_node(ctx)

    assert event.output["status"] == "FAILED"
    assert event.actions.state_delta["result_status"] == "FAIL"


def test_apply_live_node_postmaster_recorded_pending_restart():
    cfg = DBConfig(
        host="127.0.0.1",
        port=5432,
        user="u",
        password="p",
        database="d",
        db_type="postgres",
    )
    plan = {
        "knobs": [
            {
                "name": "max_connections",
                "value": "200",
                "scope": "postmaster",
                "restart_required": True,
                "reasoning": "",
            }
        ]
    }
    ctx = _FakeContext(
        {
            "dry_run": False,
            "result_status": "PASS",
            "knob_plan": plan,
            "db_config": cfg,
            "apply_mode": "dynamic",
        }
    )
    with patch(
        "src.knob_tuner.workflow.apply_knobs", return_value=[]
    ):
        event = apply_live_node(ctx)
    pending = event.output["pending_restart_knobs"]
    assert len(pending) == 1
    assert pending[0]["name"] == "max_connections"


def test_finalize_node_builds_manifest(tmp_path: Path):
    target = tmp_path / "app"
    target.mkdir()
    (target / "main.py").write_text("print('hi')\n")

    ctx = _FakeContext(
        {
            "run_id": "run-abc",
            "result_status": "PASS",
            "resource_budget": {"cpu_cores": 4, "memory_gb": 8.0},
            "db_type": "postgres",
            "db_version": "16.3",
            "target": str(target),
            "knob_plan_hash": "deadbeef",
            "sysbench_profile": {"threads": 6, "seed": 99},
            "validation_attempts": [{"attempt": 1, "status": "PASS"}],
            "applied_knobs": [{"knob": "shared_buffers"}],
            "validation_attestation": {"verified_knobs": [{"knob": "shared_buffers"}]},
            "live_result": {"status": "APPLIED"},
            "staging_issues": [],
        }
    )
    event = finalize_node(ctx)
    manifest = event.actions.state_delta["run_manifest"]
    assert manifest["run_id"] == "run-abc"
    assert manifest["status"] == "PASS"
    assert manifest["final_status"] == "PASS"
    assert manifest["seed"] == 99
    assert manifest["client_threads"] == 6
    assert manifest["attempt_count"] == 1
    assert manifest["knob_plan_hash"] == "deadbeef"
    assert manifest["application_code_hash"]
    assert event.output["run_id"] == "run-abc"


def _finalize_state(**overrides):
    state = {
        "run_id": "run-abc",
        "result_status": "PASS",
        "resource_budget": {"cpu_cores": 4, "memory_gb": 8.0},
        "db_type": "postgres",
        "target": "",
        "sysbench_profile": {},
        "staging_issues": [],
    }
    state.update(overrides)
    return state


def test_finalize_node_failed_live_apply_is_not_pass():
    ctx = _FakeContext(
        _finalize_state(
            live_result={"status": "FAILED", "reason": "apply exploded"},
        )
    )
    event = finalize_node(ctx)
    manifest = event.actions.state_delta["run_manifest"]
    assert manifest["status"] == "FAIL"
    assert manifest["final_status"] == "FAIL"
    assert any("apply exploded" in err for err in manifest["errors"])


def test_finalize_node_absent_live_apply_is_not_pass():
    ctx = _FakeContext(_finalize_state())
    event = finalize_node(ctx)
    manifest = event.actions.state_delta["run_manifest"]
    assert manifest["status"] == "INCONCLUSIVE"
    assert manifest["final_status"] == "INCONCLUSIVE"


def test_finalize_node_manual_sql_is_not_pass():
    ctx = _FakeContext(
        _finalize_state(
            live_result={"status": "MANUAL_SQL", "reason": "manual SQL emitted"},
        )
    )
    event = finalize_node(ctx)
    manifest = event.actions.state_delta["run_manifest"]
    assert manifest["status"] == "INCONCLUSIVE"
    assert manifest["status"] != "PASS"


# ===========================================================================
# 5. CLI argument parsing & strict budget parsing
# ===========================================================================


def test_cli_parser_requires_resources_and_db_name():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["/tmp/target_app"])
    with pytest.raises(SystemExit):
        parser.parse_args(["/tmp/target_app", "--db-name", "test_db"])
    with pytest.raises(SystemExit):
        parser.parse_args(["/tmp/target_app", "--db-name", "test_db", "--cpu-cores", "4"])


def test_cli_parser_defaults_with_required_resources():
    parser = build_parser()
    args = parser.parse_args(
        ["/tmp/target_app", "--db-name", "test_db", "--cpu-cores", "4", "--memory", "8"]
    )
    assert args.target == "/tmp/target_app"
    assert args.db_name == "test_db"
    assert args.model == DEFAULT_MODEL
    assert args.db_type == "postgres"
    assert args.cpu_cores == "4"
    assert args.memory == "8"
    assert args.apply_mode == "dynamic"
    assert args.results_dir == "results/dco"
    assert args.sysbench_profile is None
    assert args.db_config == "db.config"
    assert args.production_db is False
    assert args.log_file == "logs/knob_tuner.log"
    assert args.knob_path == "out/knob_tuner"
    assert args.output_path is None
    assert args.dry_run is False
    assert args.verbose is False
    assert args.cleanup_orphans is True
    assert args.buffer_time == 0.0
    assert args.multi_fidelity_min_seconds == 300.0
    assert args.screen_total_rows == 0
    assert args.screen_max_rows == 5_000_000
    assert args.screen_seconds == 10
    assert args.screen_warmup_seconds == 2
    assert args.rand_type is None
    assert args.durability_profile == "strict"
    assert args.screening_benchmark == "sysbench"
    assert args.workload_hint == ""


def test_cli_parser_custom_args():
    parser = build_parser()
    args = parser.parse_args(
        [
            "/my/codebase",
            "--db-name", "custom_db",
            "--cpu-cores", "8",
            "--memory", "16.0",
            "--apply-mode", "persist-static",
            "--results-dir", "/custom/results",
            "--sysbench-profile", "/custom/profile.json",
            "--output-path", "/custom/res.dat",
            "--dry-run",
            "-v",
            "--no-cleanup-orphans",
            "--buffer-time", "1.5",
            "--screen-seconds", "6",
            "--screen-warmup-seconds", "1",
            "--screening-benchmark", "pgbench",
            "--workload-hint", "analytical sort spills",
        ]
    )
    assert args.cpu_cores == "8"
    assert args.memory == "16.0"
    assert args.apply_mode == "persist-static"
    assert args.results_dir == "/custom/results"
    assert args.sysbench_profile == "/custom/profile.json"
    assert args.output_path == "/custom/res.dat"
    assert args.dry_run is True
    assert args.verbose is True
    assert args.cleanup_orphans is False
    assert args.buffer_time == 1.5
    assert args.screen_seconds == 6
    assert args.screen_warmup_seconds == 1
    assert args.screening_benchmark == "pgbench"
    assert args.workload_hint == "analytical sort spills"


def test_parse_budget_valid():
    budget = _parse_budget("4", "8.0")
    assert isinstance(budget, ResourceBudget)
    assert budget.cpu_cores == 4
    assert budget.memory_gb == 8.0
    assert _parse_budget(8, 16).cpu_cores == 8


@pytest.mark.parametrize("bad", ["auto", None, "", "0", "-1", "abc", "4.5"])
def test_parse_budget_rejects_bad_cpu(bad):
    with pytest.raises(ValueError):
        _parse_budget(bad, "8")


@pytest.mark.parametrize("bad", ["auto", None, "", "0", "-2.0", "bad_mem", "nan", "inf"])
def test_parse_budget_rejects_bad_memory(bad):
    with pytest.raises(ValueError):
        _parse_budget("4", bad)


@pytest.mark.parametrize("bad", [True, False])
def test_parse_budget_rejects_bools(bad):
    with pytest.raises(ValueError):
        _parse_budget(bad, "8")
    with pytest.raises(ValueError):
        _parse_budget("4", bad)


def test_load_profile_defaults_and_json(tmp_path: Path):
    assert isinstance(_load_profile(None), SysbenchProfile)

    profile_file = tmp_path / "profile.json"
    profile_file.write_text(json.dumps({"threads": 7, "seed": 3}))
    loaded = _load_profile(str(profile_file))
    assert loaded.threads == 7
    assert loaded.seed == 3

    with pytest.raises(ValueError):
        _load_profile(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError):
        _load_profile(str(bad))


# ===========================================================================
# 6. Session state initialization
# ===========================================================================


def test_build_initial_state_without_config_file():
    budget = ResourceBudget(cpu_cores=4, memory_gb=8.0)
    profile = SysbenchProfile()
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        db_name="custom_db",
        budget=budget,
        db_config_path="/tmp/non_existent.config",
        production_db=False,
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        output_path=None,
        dry_run=True,
        profile=profile,
        run_id="run-1",
        run_dir="/tmp/results/run-1",
        apply_mode="dynamic",
    )
    assert state["target"] == "/tmp/my_app"
    assert state["resource_budget"] == {"cpu_cores": 4, "memory_gb": 8.0}
    assert state["cpu_cores"] == 4
    assert state["memory_gb"] == 8.0
    assert state["sysbench_profile"] == profile.model_dump()
    assert state["sysbench_profile_hash"] == profile.profile_hash()
    assert state["apply_mode"] == "dynamic"
    assert state["run_id"] == "run-1"
    assert state["run_dir"] == "/tmp/results/run-1"
    assert state["database"] == "custom_db"
    assert "staging_db_config" not in state
    assert "db_config" not in state
    assert state["screening_benchmark"] == "sysbench"
    assert state["workload_hint"] == ""
    assert state["screen_measurement_seconds"] == 10
    assert state["screen_warmup_seconds"] == 2


def test_build_initial_state_threads_screen_measurement_settings():
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        db_name="custom_db",
        budget=ResourceBudget(cpu_cores=2, memory_gb=4.0),
        db_config_path="/tmp/non_existent.config",
        production_db=False,
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        output_path=None,
        dry_run=False,
        screen_measurement_seconds=7,
        screen_warmup_seconds=3,
    )
    assert state["screen_measurement_seconds"] == 7
    assert state["screen_warmup_seconds"] == 3


def test_build_initial_state_with_valid_config(sample_ini_path):
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        db_name="custom_db",
        budget=ResourceBudget(cpu_cores=2, memory_gb=4.0),
        db_config_path=str(sample_ini_path),
        production_db=False,
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        output_path=None,
        dry_run=False,
    )
    assert "db_config" in state
    cfg = state["db_config"]
    # Only a redacted, serializable view is stored: never the DBConfig secret.
    assert isinstance(cfg, dict)
    assert cfg["host"] == "10.0.0.2"
    assert cfg["database"] == "custom_db"
    assert "password" not in cfg
    assert "stg_pass" not in json.dumps(state, default=str)
    assert "staging_db_config" not in state
    # The target container is NEVER registered as an active staging container.
    assert cfg["restart_target"] not in ACTIVE_CONTAINERS


def test_build_initial_state_does_not_leak_password(sample_ini_path):
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="mysql",
        db_name="custom_db",
        budget=ResourceBudget(cpu_cores=2, memory_gb=4.0),
        db_config_path=str(sample_ini_path),
        production_db=True,
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        output_path=None,
        dry_run=False,
    )
    assert "password" not in state["db_config"]
    assert "password" not in state["production_db_config"]
    assert "password" not in state["prod_db_config"]
    serialized = json.dumps(state, default=str)
    assert "prod_pass" not in serialized
    assert "password" not in serialized


def test_resolve_db_config_reconstructs_secret_from_path(sample_ini_path):
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        db_name="custom_db",
        budget=ResourceBudget(cpu_cores=2, memory_gb=4.0),
        db_config_path=str(sample_ini_path),
        production_db=False,
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        output_path=None,
        dry_run=False,
    )
    assert "password" not in state["db_config"]
    cfg = _resolve_db_config(state)
    assert isinstance(cfg, DBConfig)
    assert cfg.password == "stg_pass"
    assert cfg.database == "custom_db"


def test_build_initial_state_does_not_register_target_container(sample_ini_path):
    with patch(
        "src.knob_tuner.tools.docker_tools.register_active_container"
    ) as mock_register:
        build_initial_state(
            target="/tmp/app",
            db_type="postgres",
            db_name="custom_db",
            budget=ResourceBudget(cpu_cores=2, memory_gb=4.0),
            db_config_path=str(sample_ini_path),
            production_db=False,
            log_file="/tmp/log.log",
            knob_path="/tmp/knobs",
            output_path=None,
            dry_run=False,
        )
        mock_register.assert_not_called()


def test_build_initial_state_production_env(sample_ini_path):
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="mysql",
        db_name="custom_db",
        budget=ResourceBudget(cpu_cores=4, memory_gb=16.0),
        db_config_path=str(sample_ini_path),
        production_db=True,
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        output_path=None,
        dry_run=False,
    )
    assert state["env"] == "production"
    assert state["production_db"] is True
    assert "production_db_config" in state
    assert state["production_db_config"]["host"] == "127.0.0.1"
    assert isinstance(state["production_db_config"], dict)
    assert "password" not in state["production_db_config"]


# ===========================================================================
# 7. Result writing
# ===========================================================================


def test_maybe_parse_dict_and_string():
    assert _maybe_parse({"a": 1}) == {"a": 1}
    assert _maybe_parse('{"status": "PASS"}') == {"status": "PASS"}
    assert _maybe_parse("invalid string") == {}
    assert _maybe_parse(None) == {}


def test_log_event(tmp_path: Path):
    log_file = tmp_path / "test.log"
    _log_event("Test message 1", log_file=str(log_file), verbose=False)
    content = log_file.read_text(encoding="utf-8")
    assert "Test message 1" in content


def test_write_output_result(tmp_path: Path):
    out_file = tmp_path / "subdir" / "result.json"
    state = {
        "target": "/code/app",
        "run_id": "run-1",
        "run_dir": str(tmp_path),
        "result_status": "PASS",
        "staging_validated": True,
        "resource_budget": {"cpu_cores": 4, "memory_gb": 8.0},
    }
    _write_output_result(str(out_file), state)
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["target"] == "/code/app"
    assert data["status"] == "PASS"
    assert data["staging_validated"] is True


def test_derive_status_prefers_result_status():
    assert _derive_status({"result_status": "FAIL"}) == "FAIL"
    assert _derive_status({"run_manifest": {"status": "PASS"}}) == "PASS"
    assert _derive_status({}) == "UNKNOWN"


# ===========================================================================
# 8. Pipeline execution and run-scoped artifacts
# ===========================================================================


def _mock_event():
    event = MagicMock()
    event.content.parts = [
        MagicMock(function_call=None, function_response=None, text="Completed")
    ]
    event.partial = False
    return event


def test_run_pipeline_writes_run_scoped_artifacts(tmp_path: Path):
    target_dir = tmp_path / "target_app"
    target_dir.mkdir()
    results_dir = tmp_path / "results"

    async def mock_run_async(*args, **kwargs):
        yield _mock_event()

    with patch("src.knob_tuner.main.Runner") as mock_runner_cls:
        mock_runner = MagicMock()
        mock_runner.run_async = mock_run_async
        mock_runner_cls.return_value = mock_runner

        res = asyncio.run(
            run_pipeline(
                target=str(target_dir),
                db_name="custom_db",
                model=DEFAULT_MODEL,
                db_type="postgres",
                cpu_cores_arg=2,
                memory_arg=4.0,
                log_file=str(tmp_path / "logs" / "tuner.log"),
                knob_path=str(tmp_path / "knobs"),
                results_dir=str(results_dir),
                dry_run=True,
                verbose=False,
            )
        )

    assert res["target"] == str(target_dir.resolve())
    run_dir = res["run_dir"]
    assert os.path.isdir(run_dir)
    assert Path(run_dir).parent == results_dir.resolve()

    manifest_path = os.path.join(run_dir, "manifest.json")
    result_path = os.path.join(run_dir, "result.json")
    assert os.path.isfile(manifest_path)
    assert os.path.isfile(result_path)

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    assert manifest["run_id"] == res["run_id"]
    assert manifest["resource_budget"] == {"cpu_cores": 2, "memory_gb": 4.0}
    assert manifest["final_status"] in ("PASS", "FAIL", "INCONCLUSIVE")
    assert manifest["application_target"] == str(target_dir.resolve())


def test_run_pipeline_writes_optional_output_path(tmp_path: Path):
    target_dir = tmp_path / "target_app"
    target_dir.mkdir()
    out_file = tmp_path / "extra" / "result.json"

    async def mock_run_async(*args, **kwargs):
        yield _mock_event()

    with patch("src.knob_tuner.main.Runner") as mock_runner_cls:
        mock_runner = MagicMock()
        mock_runner.run_async = mock_run_async
        mock_runner_cls.return_value = mock_runner
        asyncio.run(
            run_pipeline(
                target=str(target_dir),
                db_name="custom_db",
                cpu_cores_arg=2,
                memory_arg=4.0,
                log_file=str(tmp_path / "log.log"),
                knob_path=str(tmp_path / "knobs"),
                results_dir=str(tmp_path / "results"),
                output_path=str(out_file),
                dry_run=True,
            )
        )

    assert out_file.is_file()


def test_run_pipeline_budget_validation_precedes_side_effects(tmp_path: Path):
    target_dir = tmp_path / "target_app"
    target_dir.mkdir()

    with patch("src.knob_tuner.main.Runner") as mock_runner_cls, patch(
        "src.knob_tuner.main.cleanup_orphan_containers"
    ) as mock_cleanup, patch(
        "src.knob_tuner.main.create_run_dir"
    ) as mock_create:
        with pytest.raises(ValueError):
            asyncio.run(
                run_pipeline(
                    target=str(target_dir),
                    db_name="custom_db",
                    cpu_cores_arg="auto",
                    memory_arg="8",
                    results_dir=str(tmp_path / "results"),
                    dry_run=True,
                )
            )
        mock_runner_cls.assert_not_called()
        mock_cleanup.assert_not_called()
        mock_create.assert_not_called()


# ===========================================================================
# 9. CLI main & exit-code mapping
# ===========================================================================


def _run_main(argv, mock_state):
    with patch(
        "src.knob_tuner.main.run_pipeline", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_state
        with patch.object(sys, "argv", ["knob_tuner", *argv]):
            with pytest.raises(SystemExit) as exc_info:
                main()
    return exc_info.value.code, mock_run


def test_main_cli_invalid_target(capsys):
    with patch.object(
        sys,
        "argv",
        ["knob_tuner", "/path/that/does/not/exist", "--db-name", "custom_db", "--cpu-cores", "4", "--memory", "8"],
    ):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code == 2
    assert "ERROR: target directory not found" in capsys.readouterr().err


@pytest.mark.parametrize("bad_cpu,bad_mem", [("auto", "8"), ("4", "auto"), ("0", "8"), ("4", "-1")])
def test_main_cli_invalid_budget_exits_2(tmp_path, capsys, bad_cpu, bad_mem):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    with patch("src.knob_tuner.main.run_pipeline", new_callable=AsyncMock) as mock_run:
        with patch.object(
            sys,
            "argv",
            ["knob_tuner", str(target_dir), "--db-name", "custom_db", "--cpu-cores", bad_cpu, "--memory", bad_mem],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
    assert exc_info.value.code == 2
    mock_run.assert_not_called()
    assert "ERROR:" in capsys.readouterr().err


def test_main_cli_missing_resources_exits_2(tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    with patch.object(sys, "argv", ["knob_tuner", str(target_dir), "--db-name", "custom_db"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "status,expected",
    [("PASS", 0), ("FAIL", 1), ("INCONCLUSIVE", 3), ("UNKNOWN", 3)],
)
def test_main_cli_exit_code_mapping(tmp_path, status, expected):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    state = {"target": str(target_dir), "result_status": status, "run_dir": str(tmp_path)}
    code, _ = _run_main(
        [str(target_dir), "--db-name", "custom_db", "--cpu-cores", "4", "--memory", "8"],
        state,
    )
    assert code == expected


def test_main_cli_dry_run_exits_zero(tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    state = {
        "target": str(target_dir),
        "result_status": "INCONCLUSIVE",
        "run_dir": str(tmp_path),
    }
    code, _ = _run_main(
        [
            str(target_dir),
            "--db-name", "custom_db",
            "--cpu-cores", "4",
            "--memory", "8",
            "--dry-run",
        ],
        state,
    )
    assert code == 0


def test_main_cli_exception_exits_1(tmp_path, capsys):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    with patch(
        "src.knob_tuner.main.run_pipeline",
        side_effect=RuntimeError("Connection exploded"),
    ):
        with patch.object(
            sys,
            "argv",
            ["knob_tuner", str(target_dir), "--db-name", "custom_db", "--cpu-cores", "4", "--memory", "8"],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Knob Tuner Pipeline FAILED" in captured.err
    assert "Connection exploded" in captured.err


# ===========================================================================
# 10. Process lifecycle, safety handlers & active container tracking
# ===========================================================================


def test_register_cleanup_handlers():
    with patch("atexit.register") as mock_atexit, patch("signal.signal") as mock_signal:
        register_cleanup_handlers()
        mock_atexit.assert_called_once_with(_process_cleanup)
        assert mock_signal.call_count >= 2


def test_process_cleanup_stops_all_active_containers():
    register_active_container("cleanup_test_container_1")
    register_active_container("cleanup_test_container_2")
    assert "cleanup_test_container_1" in ACTIVE_CONTAINERS

    with patch("src.knob_tuner.main.stop_staging_db") as mock_stop:
        _process_cleanup()
        mock_stop.assert_any_call("cleanup_test_container_1")
        mock_stop.assert_any_call("cleanup_test_container_2")

    unregister_active_container("cleanup_test_container_1")
    unregister_active_container("cleanup_test_container_2")


def test_signal_handler_triggers_cleanup_and_exit():
    import signal

    with patch("src.knob_tuner.main._process_cleanup") as mock_cleanup, patch(
        "sys.exit"
    ) as mock_exit:
        _signal_handler(signal.SIGINT, None)
        mock_cleanup.assert_called_once()
        mock_exit.assert_called_once_with(128 + signal.SIGINT)


def test_run_pipeline_orphan_cleanup_called(tmp_path: Path):
    target_dir = tmp_path / "target_app"
    target_dir.mkdir()

    async def mock_run_async(*args, **kwargs):
        yield _mock_event()

    with patch(
        "src.knob_tuner.main.cleanup_orphan_containers", return_value=["stale_c1"]
    ) as mock_cleanup, patch("src.knob_tuner.main.Runner") as mock_runner_cls:
        mock_runner = MagicMock()
        mock_runner.run_async = mock_run_async
        mock_runner_cls.return_value = mock_runner
        asyncio.run(
            run_pipeline(
                target=str(target_dir),
                db_name="custom_db",
                cpu_cores_arg=2,
                memory_arg=4.0,
                results_dir=str(tmp_path / "results"),
                cleanup_orphans=True,
                dry_run=True,
            )
        )
        mock_cleanup.assert_called_once()


def test_run_pipeline_orphan_cleanup_skipped(tmp_path: Path):
    target_dir = tmp_path / "target_app"
    target_dir.mkdir()

    async def mock_run_async(*args, **kwargs):
        yield _mock_event()

    with patch(
        "src.knob_tuner.main.cleanup_orphan_containers"
    ) as mock_cleanup, patch("src.knob_tuner.main.Runner") as mock_runner_cls:
        mock_runner = MagicMock()
        mock_runner.run_async = mock_run_async
        mock_runner_cls.return_value = mock_runner
        asyncio.run(
            run_pipeline(
                target=str(target_dir),
                db_name="custom_db",
                cpu_cores_arg=2,
                memory_arg=4.0,
                results_dir=str(tmp_path / "results"),
                cleanup_orphans=False,
                dry_run=True,
            )
        )
        mock_cleanup.assert_not_called()


def test_run_pipeline_exception_triggers_cleanup(tmp_path: Path):
    target_dir = tmp_path / "target_app"
    target_dir.mkdir()

    with patch("src.knob_tuner.main.Runner") as mock_runner_cls, patch(
        "src.knob_tuner.main._process_cleanup"
    ) as mock_cleanup:
        mock_runner = MagicMock()
        mock_runner.run_async.side_effect = RuntimeError("Runner crashed")
        mock_runner_cls.return_value = mock_runner

        with pytest.raises(RuntimeError, match="Runner crashed"):
            asyncio.run(
                run_pipeline(
                    target=str(target_dir),
                    db_name="custom_db",
                    cpu_cores_arg=2,
                    memory_arg=4.0,
                    results_dir=str(tmp_path / "results"),
                    cleanup_orphans=False,
                    dry_run=True,
                )
            )
        mock_cleanup.assert_called_once()


def test_tune_loop_screen_is_time_fidelity_and_reversal_disabled():
    calls: list[dict] = []

    def validator(**kwargs):
        calls.append(kwargs)
        snapshot = kwargs.get("snapshot")
        if snapshot is not None and not snapshot.images():
            snapshot.register("key", "adco-staging-ready:test")
        return {
            "status": "PASS",
            "attestation": {"run_id": "run-1"},
            "paired": _paired([1.0, 1.0, 1.0], [8.0, 8.0, 8.0]),
            "reasons": [],
        }

    recommender = _recommender(
        {"recommendations": [{"knob": "work_mem", "recommended_value": "256MB"}]}
    )
    ctx = _FakeContext(_base_loop_state())
    with patch("src.knob_tuner.workflow.cleanup_snapshot_image") as m_clean:
        asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    assert len(calls) >= 2
    screen, confirm = calls[0], calls[-1]
    # Screening runs a shorter window without the A/B/A reversal; confirmation
    # keeps the full window and the reversal.
    assert screen["reversal"] is False
    assert confirm["reversal"] is True
    assert screen["profile"].measurement_seconds == 10
    assert screen["profile"].warmup_seconds == 2
    assert confirm["profile"].measurement_seconds == 30
    assert isinstance(screen["snapshot"], SnapshotRegistry)
    assert screen["snapshot"] is confirm["snapshot"]
    # The run-scoped snapshot is deleted before the loop returns.
    m_clean.assert_any_call("adco-staging-ready:test")
