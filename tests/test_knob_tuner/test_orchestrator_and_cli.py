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

from src.knob_tuner import create_knob_tuner_workflow, create_root_agent
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
from src.knob_tuner.workflow import (
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


def _base_loop_state(**overrides):
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
    }
    state.update(overrides)
    return state


def test_tune_loop_passes_first_attempt():
    calls = []

    def validator(**kwargs):
        calls.append(kwargs)
        return {
            "status": "PASS",
            "attestation": {"run_id": "run-1"},
            "paired": {"delta_pct": 5.0},
            "reasons": [],
        }

    recommender = _recommender(
        {"recommendations": [{"knob": "shared_buffers", "recommended_value": "1GB"}]}
    )
    ctx = _FakeContext(_base_loop_state())
    events = asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    delta = events[-1].actions.state_delta
    assert len(calls) == 1
    assert delta["result_status"] == "PASS"
    assert delta["staging_validated"] is True
    assert delta["validation_attempts"][0]["status"] == "PASS"
    assert delta["knob_plan"]["knobs"][0]["name"] == "shared_buffers"


def test_tune_loop_environment_failure_does_not_retry():
    calls = []

    def validator(**kwargs):
        calls.append(kwargs)
        return {"status": "FAIL", "paired": None, "reasons": ["env error"]}

    ctx = _FakeContext(_base_loop_state())
    events = asyncio.run(
        _drive(make_tune_loop(_recommender({"recommendations": []}), validator)(ctx))
    )

    assert len(calls) == 1
    assert events[-1].actions.state_delta["result_status"] == "FAIL"


def test_tune_loop_retries_then_passes_with_feedback():
    instructions = []
    counter = {"n": 0}

    def recommender(ctx, node_input=None):
        instructions.append(node_input)
        ctx.state["knob_recommender_output"] = {
            "recommendations": [{"knob": "work_mem", "recommended_value": "4MB"}]
        }

    def validator(**kwargs):
        counter["n"] += 1
        return {
            "status": "FAIL" if counter["n"] == 1 else "PASS",
            "paired": {"delta_pct": -9.0} if counter["n"] == 1 else {"delta_pct": 3.0},
            "reasons": ["tps dropped"] if counter["n"] == 1 else [],
        }

    ctx = _FakeContext(_base_loop_state())
    events = asyncio.run(_drive(make_tune_loop(recommender, validator)(ctx)))

    assert counter["n"] == 2
    assert len(instructions) == 2
    assert "tps dropped" in instructions[1]
    assert "delta_pct" in instructions[1]
    assert events[-1].actions.state_delta["result_status"] == "PASS"


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

    ctx = _FakeContext(_base_loop_state(dry_run=True, max_validation_attempts=1))
    events = asyncio.run(
        _drive(make_tune_loop(_recommender({"recommendations": []}), validator)(ctx))
    )

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
    assert isinstance(cfg, DBConfig)
    assert cfg.host == "10.0.0.2"
    assert cfg.database == "custom_db"
    assert "staging_db_config" not in state
    # The target container is NEVER registered as an active staging container.
    assert cfg.restart_target not in ACTIVE_CONTAINERS


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
    assert state["production_db_config"].host == "127.0.0.1"


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
        import src.knob_tuner.main as main_mod

        main_mod._cleanup_handlers_registered = False
        register_cleanup_handlers()
        assert main_mod._cleanup_handlers_registered is True
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
