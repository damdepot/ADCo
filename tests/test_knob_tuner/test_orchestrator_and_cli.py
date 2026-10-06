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
from src.knob_tuner.contracts import KnobPlan, ResourceBudget, SysbenchProfile
from src.knob_tuner.stages.models import (
    CompiledPlan,
    CompileRejection,
    DiagnosisOutput,
    ScreenVerdict,
    TerminalDecision,
)
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
from src.knob_tuner.workflow import (
    _resolve_db_config,
    apply_live_node,
    compile_candidate_node,
    confirmation_controller_node,
    decision_node,
    finalize_node,
    production_preflight_node,
    screen_candidate_node,
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
        self.route = None

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
        "prepare_run",
        "db_inspector",
        "materialize_inventory",
        "candidate_generation_agent",
        "compile_candidate",
        "screen_candidate",
        "confirmation_controller",
        "diagnosis_agent",
        "decision_node",
        "confirm_winner_node",
        "production_preflight_node",
        "apply_live_node",
        "finalize_node",
    } <= names
    assert "tune_loop" not in names


def test_workflow_edge_order():
    agent = create_root_agent()
    edges = [(e.from_node.name, e.to_node.name) for e in agent.graph.edges]
    assert edges == [
        ("__START__", "validate_budget_node"),
        ("validate_budget_node", "prepare_run"),
        ("prepare_run", "db_inspector"),
        ("db_inspector", "materialize_inventory"),
        ("materialize_inventory", "candidate_generation_agent"),
        ("candidate_generation_agent", "compile_candidate"),
        ("compile_candidate", "screen_candidate"),
        ("compile_candidate", "confirmation_controller"),
        ("screen_candidate", "diagnosis_agent"),
        ("diagnosis_agent", "confirmation_controller"),
        ("confirmation_controller", "candidate_generation_agent"),
        ("confirmation_controller", "decision_node"),
        ("decision_node", "confirm_winner_node"),
        ("confirm_winner_node", "production_preflight_node"),
        ("production_preflight_node", "apply_live_node"),
        ("apply_live_node", "finalize_node"),
    ]
    # Both screen routes (pass AND fail) point at diagnosis on one edge:
    # every screen verdict is reviewed by the diagnosis agent.
    screen_edges = [e for e in agent.graph.edges if e.from_node.name == "screen_candidate"]
    assert len(screen_edges) == 1
    assert screen_edges[0].to_node.name == "diagnosis_agent"
    assert sorted(screen_edges[0].route) == ["fail", "pass"]


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
# 3. Staged graph nodes (Wave 4: compile / control / decide / preflight)
# ===========================================================================


def _staged_state(**overrides):
    inventory = _inventory()
    state = {
        "resource_budget": {"cpu_cores": 2, "memory_gb": 4.0},
        "sysbench_profile": {},
        "apply_mode": "live",
        "dry_run": False,
        "run_id": "run-1",
        "run_dir": "/tmp/run-1",
        "db_type": "postgres",
        "database": "testdb",
        "max_attempts": 6,
        "durability_profile": "strict",
        "knobs_info": inventory,
        "available_knob_names": [k["name"] for k in inventory],
        # Keep the production default (20) out of unit tests so they
        # exercise gating, not the cap. Cap tests override explicitly.
        "max_set_knobs": 99,
        "min_improvement_pct": 2.0,
        # Quota-met by default: these tests predate the compounding campaign;
        # quota behavior is covered by dedicated quota tests.
        "success_candidates": 1,
    }
    state.update(overrides)
    return state


def _proposal(name="exp-1", phase="screen", pairs=(("work_mem", "256MB"),)):
    return {
        "name": name,
        "phase": phase,
        "levels": [
            {"knob": knob, "value": value, "reasoning": "test"}
            for knob, value in pairs
        ],
        "rationale": "test rationale",
        "objective": "test objective",
    }


def _compiled_e1():
    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    return CompiledPlan(
        plan=plan.model_dump(),
        exp_name="e1",
        phase="screen",
        valid_knobs=["work_mem"],
    )


def _confirmed_row(**overrides):
    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    row = {
        "arm": "e1",
        "phase": "screen",
        "status": "PASS",
        "mean_delta_pct": 5.0,
        "lcb_pct": 3.0,
        "ucb_pct": 7.0,
        "df": 4.0,
        "confirmed": True,
        "improvement_confident": True,
        "paired": {"baseline": {}, "tuned": {}},
        "reasons": [],
        "plan": plan,
    }
    row.update(overrides)
    return row


def test_compile_candidate_node_accepts_valid_proposal():
    ctx = _FakeContext(_staged_state())
    out = compile_candidate_node(ctx, _proposal())
    assert isinstance(out, CompiledPlan)
    assert out.exp_name == "exp-1" and out.phase == "screen"
    assert out.valid_knobs == ["work_mem"]
    assert ctx.route == "ok"
    assert ctx.state["exp_name"] == "exp-1"
    assert ctx.state["phase"] == "screen"
    assert ctx.state["n_knobs"] == 1


def test_compile_candidate_node_rejects_oversize_cap():
    ctx = _FakeContext(_staged_state(max_set_knobs=3))
    out = compile_candidate_node(
        ctx,
        _proposal(
            "big-screen",
            "screen",
            [
                ("work_mem", "256MB"),
                ("shared_buffers", "1GB"),
                ("effective_cache_size", "8GB"),
                ("max_worker_processes", "8"),
            ],
        ),
    )
    assert isinstance(out, CompileRejection)
    assert ctx.route == "rejected"
    assert "above cap" in out.reason


def test_compile_candidate_node_rejects_unknown_phase():
    ctx = _FakeContext(_staged_state())
    out = compile_candidate_node(ctx, _proposal("weird", "bogus"))
    assert isinstance(out, CompileRejection)
    assert ctx.route == "rejected"
    assert "unknown phase" in out.reason


def test_compile_candidate_node_rejects_unknown_knob_and_noop():
    ctx = _FakeContext(_staged_state())
    unknown = compile_candidate_node(
        ctx, _proposal("mystery", "screen", [("made_up_knob", "1")])
    )
    assert isinstance(unknown, CompileRejection)
    assert any("not in available knob inventory" in e for e in unknown.errors)

    # work_mem's inventory current value is 4MB: identical value is a no-op.
    noop = compile_candidate_node(
        _FakeContext(_staged_state()),
        _proposal("noop", "screen", [("work_mem", "4MB")]),
    )
    assert isinstance(noop, CompileRejection)
    assert any("no-op" in e for e in noop.errors)


def test_compile_candidate_node_durability_policy_gates_synchronous_commit():
    strict = compile_candidate_node(
        _FakeContext(_staged_state()),
        _proposal("dur", "screen", [("synchronous_commit", "off")]),
    )
    assert isinstance(strict, CompileRejection)
    assert any("durability" in e for e in strict.errors)

    # Phase 4.4: strict is enforced ALWAYS — 'relaxed' no longer permits
    # synchronous_commit=off (this assertion documents the old ambiguity).
    relaxed = compile_candidate_node(
        _FakeContext(_staged_state(durability_profile="relaxed")),
        _proposal("dur", "screen", [("synchronous_commit", "off")]),
    )
    assert isinstance(relaxed, CompileRejection)
    assert any("durability" in e for e in relaxed.errors)


def test_confirmation_controller_node_retry_then_done():
    # Pure router: the attempt was already counted once by screen_candidate;
    # the controller only routes (never recounts) on the diagnosed outcome.
    ctx = _FakeContext(
        _staged_state(
            max_attempts=3,
            validation_attempt_count=1,
            experiment_history=[
                {
                    "name": "e1",
                    "phase": "screen",
                    "n_knobs": 1,
                    "mean_delta_pct": -1.0,
                    "lcb_pct": -2.0,
                    "status": "FAIL",
                    "confirmed": False,
                }
            ],
            last_screen_row={
                "status": "FAIL",
                "mean_delta_pct": -1.0,
                "lcb_pct": -2.0,
                "reasons": ["regression"],
            },
            rejected_history=["regression"],
        )
    )
    loser = confirmation_controller_node(
        ctx,
        DiagnosisOutput(correction="drop_knob", targets=["work_mem"], rationale="bad mover", confidence=0.7),
    )
    assert loser["route"] == "retry" and ctx.route == "retry"
    assert loser["validation_attempt_count"] == 1  # unchanged: screen already counted it
    assert len(ctx.state["experiment_history"]) == 1
    assert len(ctx.state["diagnosis_history"]) == 1

    # Confident win with the quota met (1/1) stops via the backstop.
    ctx.state["last_screen_row"] = {
        "status": "PASS",
        "mean_delta_pct": 5.0,
        "lcb_pct": 3.0,
        "reasons": [],
    }
    ctx.state["experiment_history"] = [
        {
            "name": "e2",
            "phase": "screen",
            "n_knobs": 1,
            "mean_delta_pct": 5.0,
            "lcb_pct": 3.0,
            "status": "PASS",
            "confirmed": True,
        }
    ]
    champion = confirmation_controller_node(
        ctx,
        DiagnosisOutput(correction="drop_knob", targets=["work_mem"], rationale="next", confidence=0.6),
    )
    assert champion["route"] == "done" and ctx.route == "done"
    assert champion["reason"] == "confident_win_backstop"
    assert champion["validation_attempt_count"] == 1


def test_confirmation_controller_node_rejection_routes_retry():
    # Compile rejections are recorded by compile_candidate (bypassing
    # diagnosis); the controller routes without re-appending.
    ctx = _FakeContext(_staged_state(max_attempts=5, validation_attempt_count=1, experiment_history=[]))
    rej = CompileRejection(reason="bad", errors=["k: unknown"], design_name="e9")
    ctx.state["rejected_history"] = ["bad", "k: unknown"]
    out = confirmation_controller_node(ctx, rej)
    assert out["route"] == "retry"
    assert out["validation_attempt_count"] == 1
    assert ctx.state["experiment_history"] == []
    assert ctx.state["rejected_history"] == ["bad", "k: unknown"]


def test_decision_node_bridges_strong_win_to_pass():
    ctx = _FakeContext(
        _staged_state(
            all_rows=[_confirmed_row()],
            baseline_tps=[100.0],
            experiment_history=[],
            experiments_run=1,
        )
    )
    term = decision_node(ctx)
    assert isinstance(term, TerminalDecision)
    assert term.decision == "apply_winner"
    assert ctx.state["knob_plan"]["knobs"][0]["name"] == "work_mem"
    assert ctx.state["result_status"] == "PASS"
    assert ctx.state["staging_validated"] is True


def test_production_preflight_node_records_verdict():
    term = TerminalDecision(
        decision="apply_winner",
        winner_plan={"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]},
        summary={},
    )
    ctx = _FakeContext(_staged_state(result_status="PASS"))
    verdict = production_preflight_node(ctx, term)
    assert verdict.route == "auto"
    assert ctx.state["preflight_route"] == "auto"
    assert ctx.state["preflight_verdict"]["route"] == "auto"
    assert ctx.state["result_status"] == "PASS"

    blocked = TerminalDecision(decision="fail", winner_plan={"knobs": []}, summary={})
    ctx_blocked = _FakeContext(_staged_state(result_status="PASS"))
    verdict_blocked = production_preflight_node(ctx_blocked, blocked)
    assert verdict_blocked.route == "blocked"
    assert ctx_blocked.state["result_status"] == "INCONCLUSIVE"
    assert any(
        "preflight blocked" in r for r in ctx_blocked.state["staging_issues"]
    )


def test_screen_candidate_node_routes_pass_and_fail():
    compiled = _compiled_e1()
    ok_verdict = ScreenVerdict(
        status="PASS",
        mean_delta_pct=5.0,
        lcb_pct=3.0,
        confirmed=True,
        improvement_confident=True,
        paired={},
        reasons=[],
    )
    ctx = _FakeContext(_staged_state())
    with patch(
        "src.knob_tuner.workflow.stage_nodes.screen_candidate",
        return_value=ok_verdict,
    ):
        out = screen_candidate_node(ctx, compiled)
    assert out.status == "PASS"
    assert ctx.route == "pass"

    ctx_fail = _FakeContext(_staged_state())
    with patch(
        "src.knob_tuner.workflow.stage_nodes.screen_candidate",
        return_value=ScreenVerdict(status="FAIL", mean_delta_pct=-1.0),
    ):
        out_fail = screen_candidate_node(ctx_fail, compiled)
    assert ctx_fail.route == "fail"
    assert out_fail.confirmed is False


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
            "apply_mode": "live",
        }
    )
    applied = [{"knob": "shared_buffers", "value": "1GB", "status": "applied"}]
    with patch("src.knob_tuner.workflow.apply_knobs", return_value=applied) as mock_apply:
        event = apply_live_node(ctx)
        mock_apply.assert_called_once()
    assert event.output["status"] == "APPLIED"
    assert event.actions.state_delta["applied_knobs"] == applied


def test_apply_live_node_manual_emits_manual_sql():
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
            "apply_mode": "manual",
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


def test_apply_live_node_preflight_manual_gates_live_mutation():
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
            "apply_mode": "live",
            "preflight_route": "maintenance_assisted",
            "preflight_verdict": {
                "route": "maintenance_assisted",
                "reason": "restart-required knobs (shared_buffers)",
            },
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
    assert event.actions.state_delta["result_status"] == "INCONCLUSIVE"
    assert mock_apply.call_args.kwargs.get("dry_run") is True


def test_apply_live_node_preflight_blocked_applies_nothing():
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
            "apply_mode": "live",
            "preflight_route": "blocked",
            "preflight_verdict": {"route": "blocked", "reason": "empty plan"},
        }
    )
    with patch("src.knob_tuner.workflow.apply_knobs") as mock_apply:
        event = apply_live_node(ctx)
        mock_apply.assert_not_called()

    assert event.output["status"] == "FAILED"
    assert "preflight blocked" in event.output["reason"]
    assert event.output["applied_knobs"] == []
    assert event.actions.state_delta["applied_knobs"] == []
    assert event.actions.state_delta["result_status"] == "FAIL"


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
            "apply_mode": "live",
        }
    )
    with patch(
        "src.knob_tuner.workflow.apply_knobs", side_effect=RuntimeError("boom")
    ):
        event = apply_live_node(ctx)

    assert event.output["status"] == "FAILED"
    assert event.actions.state_delta["result_status"] == "FAIL"


def test_apply_live_node_manual_sql_failure_sets_result_status_fail():
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
            "apply_mode": "manual",
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
            "apply_mode": "live",
        }
    )
    applied = [
        {
            "knob": "max_connections",
            "value": "200",
            "status": "applied",
            "sql": "ALTER SYSTEM SET max_connections = '200';",
            "error": None,
        }
    ]
    with patch(
        "src.knob_tuner.workflow.apply_knobs", return_value=applied
    ):
        event = apply_live_node(ctx)
    pending = event.output["pending_restart_knobs"]
    assert len(pending) == 1
    assert pending[0]["name"] == "max_connections"
    # LIVE applies the full plan: the postmaster knob is persisted now and
    # recorded as pending an operator restart, so this is a full apply.
    persisted = event.output["persisted_static_knobs"]
    assert len(persisted) == 1
    assert persisted[0]["knob"] == "max_connections"
    assert event.output["status"] == "APPLIED"
    assert event.output["applied_knobs"] == applied
    assert "result_status" not in event.actions.state_delta


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
# 4b. Explicit apply outcomes (Phase 1.4) + staging identity gate (Phase 1.5)
# ===========================================================================


def _live_cfg(database="d", host="127.0.0.1", port=5432):
    return DBConfig(
        host=host,
        port=port,
        user="u",
        password="p",
        database=database,
        db_type="postgres",
    )


def _pass_ctx(**overrides):
    state = {
        "dry_run": False,
        "result_status": "PASS",
        "knob_plan": _plan_dump(),
        "db_config": _live_cfg(),
        "apply_mode": "live",
    }
    state.update(overrides)
    return _FakeContext(state)


def test_apply_live_node_all_failed_yields_applied_nothing_and_fail():
    ctx = _pass_ctx()
    failed = [
        {
            "knob": "shared_buffers",
            "value": "1GB",
            "status": "failed",
            "sql": "",
            "error": "boom",
        }
    ]
    with patch("src.knob_tuner.workflow.apply_knobs", return_value=failed):
        event = apply_live_node(ctx)
    assert event.output["status"] == "APPLIED_NOTHING"
    assert event.output["applied_knobs"] == []
    assert event.actions.state_delta["result_status"] == "FAIL"


def test_apply_live_node_all_skipped_yields_applied_nothing_inconclusive():
    ctx = _pass_ctx()
    skipped = [
        {
            "knob": "shared_buffers",
            "value": "1GB",
            "status": "skipped",
            "sql": "",
            "error": None,
        }
    ]
    with patch("src.knob_tuner.workflow.apply_knobs", return_value=skipped):
        event = apply_live_node(ctx)
    assert event.output["status"] == "APPLIED_NOTHING"
    assert event.output["applied_knobs"] == []
    assert event.actions.state_delta["result_status"] == "INCONCLUSIVE"


def test_apply_live_node_mode_none_applies_nothing():
    # Real apply_knobs under NONE never touches the DB (all skipped).
    ctx = _pass_ctx(apply_mode="none")
    event = apply_live_node(ctx)
    assert event.output["status"] == "APPLIED_NOTHING"
    assert event.output["applied_knobs"] == []
    assert event.actions.state_delta["result_status"] == "INCONCLUSIVE"


def test_apply_live_node_empty_plan_applies_nothing():
    ctx = _pass_ctx(knob_plan={"knobs": []})
    with patch("src.knob_tuner.workflow.apply_knobs", return_value=[]):
        event = apply_live_node(ctx)
    assert event.output["status"] == "APPLIED_NOTHING"
    assert event.actions.state_delta["result_status"] == "INCONCLUSIVE"


def test_apply_live_node_partial_downgrades_to_inconclusive():
    ctx = _pass_ctx()
    mixed = [
        {"knob": "work_mem", "value": "64MB", "status": "applied"},
        {"knob": "shared_buffers", "value": "1GB", "status": "failed", "error": "x"},
    ]
    with patch("src.knob_tuner.workflow.apply_knobs", return_value=mixed):
        event = apply_live_node(ctx)
    assert event.output["status"] == "PARTIAL"
    assert len(event.output["applied_knobs"]) == 1
    assert event.actions.state_delta["result_status"] == "INCONCLUSIVE"


def test_apply_live_node_full_apply_keeps_pass():
    ctx = _pass_ctx()
    applied = [{"knob": "shared_buffers", "value": "1GB", "status": "applied"}]
    with patch("src.knob_tuner.workflow.apply_knobs", return_value=applied):
        event = apply_live_node(ctx)
    assert event.output["status"] == "APPLIED"
    assert "result_status" not in event.actions.state_delta


def test_apply_live_node_real_apply_persists_postmaster(mock_db_conn):
    # End-to-end through the real apply_knobs: under LIVE a restart-required
    # (postmaster) knob is persisted now and recorded for the next restart,
    # while the reloadable knob is applied live. The tuner never restarts.
    conn, cursor = mock_db_conn
    plan = {
        "knobs": [
            {
                "name": "work_mem",
                "value": "64MB",
                "scope": "user",
                "restart_required": False,
                "reasoning": "",
            },
            {
                "name": "shared_buffers",
                "value": "1GB",
                "scope": "postmaster",
                "restart_required": True,
                "reasoning": "",
            },
        ]
    }
    ctx = _pass_ctx(knob_plan=plan)
    with patch("src.knob_tuner.tools.db_tools.get_connection", return_value=conn):
        event = apply_live_node(ctx)

    assert event.output["status"] == "APPLIED"
    applied_names = [r["knob"] for r in event.output["applied_knobs"]]
    assert applied_names == ["work_mem", "shared_buffers"]
    persisted = event.output["persisted_static_knobs"]
    assert [r["knob"] for r in persisted] == ["shared_buffers"]
    pending = event.output["pending_restart_knobs"]
    assert [p["name"] for p in pending] == ["shared_buffers"]


def test_apply_live_node_identity_mismatch_refuses():
    ctx = _pass_ctx(
        staging_database_identity="postgres://127.0.0.1:9999/staging_db",
    )
    with patch("src.knob_tuner.workflow.apply_knobs") as mock_apply:
        event = apply_live_node(ctx)
        mock_apply.assert_not_called()
    assert event.output["status"] == "FAILED"
    assert "mismatch" in event.output["reason"]
    assert event.output["applied_knobs"] == []
    assert event.actions.state_delta["result_status"] == "FAIL"


def test_apply_live_node_identity_match_proceeds():
    # Same database name, ephemeral staging host/port: the (engine, db) pair
    # matches, so the apply proceeds.
    ctx = _pass_ctx(
        staging_database_identity="postgres://10.9.8.7:23456/d",
    )
    applied = [{"knob": "shared_buffers", "value": "1GB", "status": "applied"}]
    with patch(
        "src.knob_tuner.workflow.apply_knobs", return_value=applied
    ) as mock_apply:
        event = apply_live_node(ctx)
        mock_apply.assert_called_once()
    assert event.output["status"] == "APPLIED"


def test_apply_live_node_attestation_identity_fallback_refuses():
    ctx = _pass_ctx(
        validation_attestation={"database_identity": "postgres://h:1/other_db"},
    )
    with patch("src.knob_tuner.workflow.apply_knobs") as mock_apply:
        event = apply_live_node(ctx)
        mock_apply.assert_not_called()
    assert event.output["status"] == "FAILED"
    assert event.actions.state_delta["result_status"] == "FAIL"


def test_apply_live_node_no_staging_record_proceeds():
    ctx = _pass_ctx()
    applied = [{"knob": "shared_buffers", "value": "1GB", "status": "applied"}]
    with patch(
        "src.knob_tuner.workflow.apply_knobs", return_value=applied
    ) as mock_apply:
        event = apply_live_node(ctx)
        mock_apply.assert_called_once()
    assert event.output["status"] == "APPLIED"


def test_screen_candidate_records_staging_identity():
    from src.knob_tuner.stages import nodes as stage_nodes
    from src.knob_tuner.stages.models import CompiledPlan as _CompiledPlan

    def _validate_with_attestation(**kwargs):
        return {
            "status": "PASS",
            "paired": {
                "baseline": {"per_run_tps": [100.0, 101.0, 102.0, 99.0, 100.5]},
                "tuned": {"per_run_tps": [120.0, 121.0, 119.0, 122.0, 120.5]},
            },
            "reasons": [],
            "stopped_early": False,
            "attestation": {
                "database_identity": "postgres://127.0.0.1:5555/testdb"
            },
        }

    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    compiled = _CompiledPlan(
        plan=plan.model_dump(),
        exp_name="e1",
        phase="screen",
        valid_knobs=["work_mem"],
    )
    ctx = _FakeContext({"min_improvement_pct": 5.0})
    stage_nodes.screen_candidate(
        ctx, compiled, validate_fn=_validate_with_attestation
    )
    assert (
        ctx.state["staging_database_identity"]
        == "postgres://127.0.0.1:5555/testdb"
    )


def test_finalize_node_applied_nothing_with_failures_is_fail():
    ctx = _FakeContext(
        _finalize_state(
            live_result={
                "status": "APPLIED_NOTHING",
                "reason": "every knob failed (1/1): bad_knob",
                "results": [{"knob": "bad_knob", "status": "failed"}],
            },
        )
    )
    event = finalize_node(ctx)
    manifest = event.actions.state_delta["run_manifest"]
    assert manifest["status"] == "FAIL"
    assert manifest["final_status"] == "FAIL"
    assert any("APPLIED_NOTHING" in err for err in manifest["errors"])


def test_finalize_node_applied_nothing_all_skipped_is_inconclusive():
    ctx = _FakeContext(
        _finalize_state(
            live_result={
                "status": "APPLIED_NOTHING",
                "reason": "every knob skipped under live apply mode",
                "results": [{"knob": "shared_buffers", "status": "skipped"}],
            },
        )
    )
    event = finalize_node(ctx)
    manifest = event.actions.state_delta["run_manifest"]
    assert manifest["status"] == "INCONCLUSIVE"
    assert manifest["status"] != "PASS"


def test_finalize_node_partial_is_not_pass():
    ctx = _FakeContext(
        _finalize_state(
            applied_knobs=[{"knob": "work_mem"}],
            live_result={
                "status": "PARTIAL",
                "reason": "partial apply: 1/2 knobs applied",
                "applied_knobs": [{"knob": "work_mem"}],
                "results": [
                    {"knob": "work_mem", "status": "applied"},
                    {"knob": "shared_buffers", "status": "failed"},
                ],
            },
        )
    )
    event = finalize_node(ctx)
    manifest = event.actions.state_delta["run_manifest"]
    assert manifest["status"] == "INCONCLUSIVE"
    assert manifest["status"] != "PASS"


def test_finalize_node_legacy_completed_empty_is_not_pass():
    ctx = _FakeContext(
        _finalize_state(
            live_result={"status": "COMPLETED", "reason": "", "results": []},
        )
    )
    event = finalize_node(ctx)
    manifest = event.actions.state_delta["run_manifest"]
    assert manifest["status"] != "PASS"
    assert manifest["status"] == "INCONCLUSIVE"


# ===========================================================================
# 4b-ii. keep_best-after-futility gate (Phase 1.7)
# ===========================================================================


def _weak_row(**overrides):
    """Confirmed PASS row below the win threshold (a keep_best candidate)."""
    row = _confirmed_row(
        mean_delta_pct=1.0,
        lcb_pct=0.5,
        ucb_pct=1.5,
        improvement_confident=False,
    )
    row.update(overrides)
    return row


def _fail_row(arm="e1", **overrides):
    row = _confirmed_row(
        status="FAIL",
        mean_delta_pct=-1.0,
        lcb_pct=-2.0,
        ucb_pct=0.0,
        confirmed=False,
        improvement_confident=False,
        reasons=["regression"],
    )
    row["arm"] = arm
    row.update(overrides)
    return row


def _futility_state(**overrides):
    state = _staged_state(
        baseline_tps=[100.0, 101.0],
        diagnosis_output={
            "correction": "stop",
            "stop_reason": "futility",
            "confidence": 0.8,
        },
    )
    state.update(overrides)
    return state


def test_decision_withholds_confirmed_pass_below_threshold():
    from src.knob_tuner.stages import nodes as stage_nodes

    # Compounding quota unification: "winner" means lcb > min_improvement_pct,
    # so a confirmed PASS whose LCB misses the bar withholds.
    ctx = _FakeContext(_futility_state(all_rows=[_weak_row()]))

    dec = stage_nodes.decision(ctx)
    assert dec.decision != "apply_winner", dec.summary
    assert dec.winner_plan == {}


def test_decision_withholds_unconfirmed_best():
    from src.knob_tuner.stages import nodes as stage_nodes

    ctx = _FakeContext(_futility_state(all_rows=[_weak_row(confirmed=False)]))

    dec = stage_nodes.decision(ctx)
    assert dec.decision in ("inconclusive", "fail"), dec.summary
    assert dec.winner_plan == {}


def test_decision_withholds_confirmed_nonpass_best():
    from src.knob_tuner.stages import nodes as stage_nodes

    ctx = _FakeContext(_futility_state(all_rows=[_weak_row(status="FAIL")]))

    dec = stage_nodes.decision(ctx)
    assert dec.decision in ("inconclusive", "fail"), dec.summary
    assert dec.winner_plan == {}
    assert any("withheld" in r for r in dec.summary["reasons"])


def test_decision_node_downgrades_stray_keep_best_without_flag():
    keep = TerminalDecision(
        decision="keep_best",
        winner_plan={
            "knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]
        },
        summary={"reasons": []},
    )
    ctx = _FakeContext(_futility_state(all_rows=[]))
    with patch(
        "src.knob_tuner.workflow.stage_nodes.decision", return_value=keep
    ):
        term = decision_node(ctx)
    assert term.decision == "inconclusive"
    assert term.winner_plan == {}
    assert ctx.state["knob_plan"] == {"knobs": []}
    assert ctx.state["result_status"] == "INCONCLUSIVE"


def test_futility_campaign_applies_nothing():
    """Key Phase 1.7 assertion: all-FAIL screens apply nothing, never PASS."""
    ctx = _FakeContext(
        _futility_state(all_rows=[_fail_row("e1"), _fail_row("e2")])
    )
    term = decision_node(ctx)
    assert term.decision in ("inconclusive", "fail"), term.summary
    assert term.winner_plan == {}
    assert ctx.state["knob_plan"] == {"knobs": []}
    assert ctx.state["result_status"] in ("INCONCLUSIVE", "FAIL")

    verdict = production_preflight_node(ctx, term)
    assert verdict.route == "blocked"

    with patch("src.knob_tuner.workflow.apply_knobs") as mock_apply:
        event = apply_live_node(ctx)
        mock_apply.assert_not_called()
    assert event.output["status"] == "APPLIED_NOTHING"
    assert event.output["applied_knobs"] == []
    ctx.state.update(event.actions.state_delta)

    final = finalize_node(ctx)
    manifest = final.actions.state_delta["run_manifest"]
    assert manifest["status"] != "PASS"
    assert manifest["applied_knobs"] == []


def test_apply_live_node_empty_plan_reports_applied_nothing():
    ctx = _FakeContext(
        {
            "dry_run": False,
            "result_status": "INCONCLUSIVE",
            "knob_plan": {"knobs": []},
        }
    )
    with patch("src.knob_tuner.workflow.apply_knobs") as mock_apply:
        event = apply_live_node(ctx)
        mock_apply.assert_not_called()
    assert event.output["status"] == "APPLIED_NOTHING"
    assert event.output["applied_knobs"] == []


# ===========================================================================
# 4c. CLI Next-Steps message + exit code (Phase 1.6)
# ===========================================================================


def _cli_result(tmp_path, live_result, result_status="PASS"):
    return {
        "target": str(tmp_path),
        "result_status": result_status,
        "run_dir": str(tmp_path),
        "staging_issues": [],
        "live_result": live_result,
        "run_manifest": {},
    }


def _cli_argv(tmp_path, *extra):
    return [str(tmp_path), "--db-name", "custom_db", "--cpu-cores", "4", "--memory", "8", *extra]


def test_main_cli_next_steps_reports_persisted(tmp_path, capsys):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    live = {
        "status": "APPLIED",
        "reason": "",
        "applied_knobs": [{"knob": "shared_buffers", "status": "applied"}],
        "persisted_static_knobs": [{"knob": "shared_buffers"}],
        "pending_restart_knobs": [{"name": "shared_buffers"}],
        "results": [{"knob": "shared_buffers", "status": "applied"}],
    }
    with patch(
        "src.knob_tuner.main.run_pipeline", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = _cli_result(tmp_path, live)
        with patch.object(sys, "argv", ["knob_tuner", *_cli_argv(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "have been persisted" in out


def test_main_cli_next_steps_reports_skipped_under_live(tmp_path, capsys):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    live = {
        "status": "APPLIED",
        "reason": "",
        "applied_knobs": [{"knob": "work_mem", "status": "applied"}],
        "persisted_static_knobs": [],
        "pending_restart_knobs": [{"name": "shared_buffers"}],
        "results": [
            {"knob": "work_mem", "status": "applied"},
            {"knob": "shared_buffers", "status": "skipped"},
        ],
    }
    with patch(
        "src.knob_tuner.main.run_pipeline", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = _cli_result(tmp_path, live)
        with patch.object(sys, "argv", ["knob_tuner", *_cli_argv(tmp_path)]):
            with pytest.raises(SystemExit):
                main()
    out = capsys.readouterr().out
    assert "skipped under live apply mode" in out
    assert "have been persisted" not in out


def test_main_cli_next_steps_reports_nothing_applied(tmp_path, capsys):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    live = {
        "status": "APPLIED_NOTHING",
        "reason": "every knob skipped under live apply mode (1/1)",
        "applied_knobs": [],
        "persisted_static_knobs": [],
        "pending_restart_knobs": [{"name": "shared_buffers"}],
        "results": [{"knob": "shared_buffers", "status": "skipped"}],
    }
    with patch(
        "src.knob_tuner.main.run_pipeline", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = _cli_result(
            tmp_path, live, result_status="INCONCLUSIVE"
        )
        with patch.object(sys, "argv", ["knob_tuner", *_cli_argv(tmp_path)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
    assert exc_info.value.code == 3
    out = capsys.readouterr().out
    assert "were NOT persisted" in out
    assert "have been persisted" not in out


def test_main_cli_dry_run_fail_exits_1(tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    state = {
        "target": str(target_dir),
        "result_status": "FAIL",
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
    assert code == 1


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
    assert args.apply_mode == "live"
    assert args.results_dir == "results/dco"
    assert args.db_config == "db.config"
    assert not hasattr(args, "production_db")
    assert args.success_candidates == 10
    assert args.max_attempts == 20
    assert args.min_improvement_pct == 5.0
    assert args.log_file == "logs/knob_tuner.log"
    assert args.dry_run is False
    assert args.verbose is False
    assert args.buffer_time == 0.0
    option_strings = {s for a in parser._actions for s in a.option_strings}
    for removed in (
        "--apply-unconfirmed",
        "--durability-profile",
        "--workload-hint",
        "--rand-type",
        "--sysbench-profile",
        "--no-cleanup-orphans",
        "--output-path",
        "--knob-path",
        "--screen-seconds",
        "--screen-warmup-seconds",
        "--candidate-repetitions",
        "--candidate-seconds",
        "--candidate-warmup-seconds",
        "--repetitions",
        "--production-db",
    ):
        assert removed not in option_strings
    assert "--measure-reps" in option_strings
    assert "--multi-fidelity-min-seconds" not in option_strings
    assert not hasattr(args, "multi_fidelity_min_seconds")
    assert not hasattr(args, "screen_seconds")
    assert not hasattr(args, "screen_warmup_seconds")
    assert args.screen_total_rows == 0
    assert args.screen_max_rows == 5_000_000
    assert args.measure_reps == 10
    assert args.measure_seconds == 10
    assert args.measure_warmup_seconds == 2
    assert args.early_stop_min_reps == 4
    assert args.screening_benchmark == "sysbench"
    assert args.max_attempts == 20
    assert args.success_candidates == 10


def test_cli_parser_max_attempts_override_and_no_legacy_flags():
    parser = build_parser()
    args = parser.parse_args(
        ["/tmp/target_app", "--db-name", "test_db", "--cpu-cores", "4", "--memory", "8",
         "--max-attempts", "7"]
    )
    assert args.max_attempts == 7
    option_strings = {s for a in parser._actions for s in a.option_strings}
    assert "--max-attempts" in option_strings
    assert "--max-validation-attempts" not in option_strings
    assert "--max-experiments" not in option_strings
    assert "--multi-fidelity-min-seconds" not in option_strings


def test_build_initial_state_drops_multi_fidelity_min_seconds():
    budget = ResourceBudget(cpu_cores=4, memory_gb=8.0)
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        db_name="custom_db",
        budget=budget,
        db_config_path="/tmp/non_existent.config",
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        dry_run=True,
    )
    assert "multi_fidelity_min_seconds" not in state


def test_durability_always_strict_in_initial_state():
    budget = ResourceBudget(cpu_cores=4, memory_gb=8.0)
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        db_name="custom_db",
        budget=budget,
        db_config_path="/tmp/non_existent.config",
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        dry_run=True,
    )
    assert state["durability_profile"] == "strict"


def test_build_initial_state_sets_single_attempt_ceiling():
    budget = ResourceBudget(cpu_cores=4, memory_gb=8.0)
    default = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        db_name="custom_db",
        budget=budget,
        db_config_path="/tmp/non_existent.config",
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        dry_run=True,
    )
    assert default["max_attempts"] == 20
    assert default["success_candidates"] == 10
    assert "max_validation_attempts" not in default
    assert "max_experiments" not in default
    custom = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        db_name="custom_db",
        budget=budget,
        db_config_path="/tmp/non_existent.config",
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        dry_run=True,
        max_attempts=7,
    )
    assert custom["max_attempts"] == 7


def test_cli_parser_custom_args():
    parser = build_parser()
    args = parser.parse_args(
        [
            "/my/codebase",
            "--db-name", "custom_db",
            "--cpu-cores", "8",
            "--memory", "16.0",
            "--apply-mode", "manual",
            "--results-dir", "/custom/results",
            "--dry-run",
            "-v",
            "--buffer-time", "1.5",
            "--measure-reps", "6",
            "--measure-seconds", "6",
            "--measure-warmup-seconds", "1",
            "--early-stop-min-reps", "3",
            "--screening-benchmark", "pgbench",
        ]
    )
    assert args.cpu_cores == "8"
    assert args.memory == "16.0"
    assert args.apply_mode == "manual"
    assert args.results_dir == "/custom/results"
    assert args.dry_run is True
    assert args.verbose is True
    assert args.buffer_time == 1.5
    assert args.measure_reps == 6
    assert args.measure_seconds == 6
    assert args.measure_warmup_seconds == 1
    assert args.early_stop_min_reps == 3
    assert args.screening_benchmark == "pgbench"


def test_cli_parser_apply_mode_choices_are_canonical_only():
    parser = build_parser()
    action = next(a for a in parser._actions if "--apply-mode" in a.option_strings)
    assert sorted(action.choices) == ["live", "manual", "none"]
    # Legacy spellings are hidden from the CLI but still coerce via state.
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["/tmp/target_app", "--db-name", "t", "--cpu-cores", "4",
             "--memory", "8", "--apply-mode", "persist-static"]
        )


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
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        dry_run=True,
        profile=profile,
        run_id="run-1",
        run_dir="/tmp/results/run-1",
        apply_mode="live",
    )
    assert state["target"] == "/tmp/my_app"
    assert state["resource_budget"] == {"cpu_cores": 4, "memory_gb": 8.0}
    assert state["cpu_cores"] == 4
    assert state["memory_gb"] == 8.0
    assert state["sysbench_profile"] == profile.model_dump()
    assert state["sysbench_profile_hash"] == profile.profile_hash()
    assert state["apply_mode"] == "live"
    assert state["run_id"] == "run-1"
    assert state["run_dir"] == "/tmp/results/run-1"
    assert state["database"] == "custom_db"
    assert "staging_db_config" not in state
    assert "db_config" not in state
    assert state["screening_benchmark"] == "sysbench"
    assert state["workload_hint"] == ""
    assert state["measure_reps"] == 10
    assert state["measure_seconds"] == 10
    assert state["measure_warmup_seconds"] == 2
    assert state["early_stop_min_reps"] == 4
    for dead_key in (
        "screen_measurement_seconds",
        "screen_warmup_seconds",
        "candidate_repetitions",
        "candidate_measurement_seconds",
        "candidate_warmup_seconds",
    ):
        assert dead_key not in state


def test_build_initial_state_threads_timing_settings():
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        db_name="custom_db",
        budget=ResourceBudget(cpu_cores=2, memory_gb=4.0),
        db_config_path="/tmp/non_existent.config",
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        dry_run=False,
        measure_reps=7,
        measure_seconds=8,
        measure_warmup_seconds=3,
        early_stop_min_reps=5,
    )
    assert state["measure_reps"] == 7
    assert state["measure_seconds"] == 8
    assert state["measure_warmup_seconds"] == 3
    assert state["early_stop_min_reps"] == 5


def test_build_initial_state_clamps_timing_settings():
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        db_name="custom_db",
        budget=ResourceBudget(cpu_cores=2, memory_gb=4.0),
        db_config_path="/tmp/non_existent.config",
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        dry_run=False,
        measure_reps=0,
        measure_seconds=0,
        measure_warmup_seconds=0,
        early_stop_min_reps=0,
    )
    assert state["measure_reps"] == 2
    assert state["measure_seconds"] == 1
    assert state["measure_warmup_seconds"] == 0
    assert state["early_stop_min_reps"] == 2


def test_build_initial_state_with_valid_config(sample_ini_path):
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        db_name="custom_db",
        budget=ResourceBudget(cpu_cores=2, memory_gb=4.0),
        db_config_path=str(sample_ini_path),
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
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
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        dry_run=False,
    )
    assert "password" not in state["db_config"]
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
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
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
            log_file="/tmp/log.log",
            knob_path="/tmp/knobs",
            dry_run=False,
        )
        mock_register.assert_not_called()


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
                results_dir=str(results_dir),
                dry_run=True,
                verbose=False,
            )
        )

    assert res["target"] == str(target_dir.resolve())
    run_dir = res["run_dir"]
    assert os.path.isdir(run_dir)
    assert Path(run_dir).parent == results_dir.resolve()

    # Knob artifacts are derived under the run directory.
    assert res["knob_path"] == os.path.join(run_dir, "knobs")
    assert os.path.isdir(res["knob_path"])

    manifest_path = os.path.join(run_dir, "manifest.json")
    result_path = os.path.join(run_dir, "result.json")
    assert os.path.isfile(manifest_path)
    assert os.path.isfile(result_path)

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    assert manifest["run_id"] == res["run_id"]
    assert manifest["resource_budget"] == {"cpu_cores": 2, "memory_gb": 4.0}
    assert manifest["final_status"] in ("PASS", "FAIL", "INCONCLUSIVE")
    assert manifest["application_target"] == str(target_dir.resolve())


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_run_pipeline_refreshes_latest_symlink(tmp_path: Path):
    target_dir = tmp_path / "target_app"
    target_dir.mkdir()
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    # A stale pointer that must be replaced by the new run.
    stale = results_dir / "latest"
    try:
        os.symlink(str(results_dir), str(stale))
    except OSError:
        pytest.skip("symlinks not permitted on this platform")

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
                cpu_cores_arg=2,
                memory_arg=4.0,
                log_file=str(tmp_path / "log.log"),
                results_dir=str(results_dir),
                dry_run=True,
            )
        )

    latest = results_dir / "latest"
    assert os.path.islink(latest)
    assert Path(os.path.realpath(latest)) == Path(res["run_dir"]).resolve()


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
                dry_run=True,
            )
        )
        mock_cleanup.assert_called_once()


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
                    dry_run=True,
                )
            )
        mock_cleanup.assert_called_once()
