"""Tests for knob_tuner orchestrator agent, CLI arg parsing, session initialization, and runner."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool

from src.knob_tuner import create_root_agent
from src.knob_tuner.agent import ORCHESTRATOR_PROMPT
from src.knob_tuner.main import (
    DEFAULT_MODEL,
    _log_event,
    _maybe_parse,
    _parse_cpu_cores,
    _parse_memory,
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
from src.knob_tuner.tools.docker_tools import (
    ACTIVE_CONTAINERS,
    cleanup_orphan_containers,
    register_active_container,
    stop_staging_db,
    unregister_active_container,
)


# ===========================================================================
# 1. Orchestrator Agent Initialization & Prompt Tests
# ===========================================================================

def test_orchestrator_agent_default_initialization():
    agent = create_root_agent()
    assert isinstance(agent, LlmAgent)
    assert agent.name == "knob_tuner"
    assert agent.model == "gemini-3.5-flash-lite"
    assert "ADCo Knob Tuner Orchestrator" in agent.instruction
    assert len(agent.tools) == 4

    # Verify all 4 sub-agents are registered as AgentTools
    sub_agent_names = []
    for tool in agent.tools:
        assert isinstance(tool, AgentTool)
        sub_agent_names.append(tool.agent.name)

    assert "intent_analyzer" in sub_agent_names
    assert "knob_recommender" in sub_agent_names
    assert "knob_checker" in sub_agent_names
    assert "live_tuner" in sub_agent_names


def test_orchestrator_agent_custom_model():
    agent = create_root_agent(model="gemini-1.5-pro")
    assert agent.model == "gemini-1.5-pro"
    for tool in agent.tools:
        if isinstance(tool, AgentTool):
            assert tool.agent.model == "gemini-1.5-pro"


def test_orchestrator_prompt_contains_rules_and_loop_bounds():
    assert "intent_analyzer" in ORCHESTRATOR_PROMPT
    assert "knob_recommender" in ORCHESTRATOR_PROMPT
    assert "knob_checker" in ORCHESTRATOR_PROMPT
    assert "live_tuner" in ORCHESTRATOR_PROMPT
    assert "4" in ORCHESTRATOR_PROMPT  # max 4 attempts (1 initial + 3 retries)
    assert "PASS" in ORCHESTRATOR_PROMPT
    assert "FAIL" in ORCHESTRATOR_PROMPT
    assert "5-step" in ORCHESTRATOR_PROMPT
    assert "sysbench" in ORCHESTRATOR_PROMPT
    assert "performance regression" in ORCHESTRATOR_PROMPT
    assert "tuned TPS < baseline TPS" in ORCHESTRATOR_PROMPT
    assert "benchmark delta" in ORCHESTRATOR_PROMPT


# ===========================================================================
# 2. CLI Argument Parsing Tests
# ===========================================================================

def test_cli_parser_defaults():
    parser = build_parser()
    args = parser.parse_args(["/tmp/target_app"])
    assert args.target == "/tmp/target_app"
    assert args.model == "gemini-3.5-flash-lite"
    assert args.db_type == "postgres"
    assert args.cpu_cores == "auto"
    assert args.memory == "auto"
    assert args.db_config == "db.config"
    assert args.production_db is False
    assert args.log_file == "logs/knob_tuner.log"
    assert args.knob_path == "out/knob_tuner"
    assert args.output_path == "out/knob_tuner/result.json"
    assert args.dry_run is False
    assert args.verbose is False
    assert args.cleanup_orphans is True


def test_cli_parser_custom_args():
    parser = build_parser()
    args = parser.parse_args([
        "/my/codebase",
        "--model", "gemini-1.5-flash",
        "--db-type", "mysql",
        "--cpu-cores", "8",
        "--memory", "16.0",
        "--db-config", "custom_db.config",
        "--production-db",
        "--log-file", "/custom/logs.log",
        "--knob-path", "/custom/knobs",
        "--output-path", "/custom/res.dat",
        "--dry-run",
        "-v",
        "--no-cleanup-orphans",
    ])
    assert args.target == "/my/codebase"
    assert args.model == "gemini-1.5-flash"
    assert args.db_type == "mysql"
    assert args.cpu_cores == "8"
    assert args.memory == "16.0"
    assert args.db_config == "custom_db.config"
    assert args.production_db is True
    assert args.log_file == "/custom/logs.log"
    assert args.knob_path == "/custom/knobs"
    assert args.output_path == "/custom/res.dat"
    assert args.dry_run is True
    assert args.verbose is True
    assert args.cleanup_orphans is False


def test_parse_cpu_cores():
    assert _parse_cpu_cores("auto") >= 1
    assert _parse_cpu_cores(None) >= 1
    assert _parse_cpu_cores("4") == 4
    assert _parse_cpu_cores(8) == 8

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_cpu_cores("invalid")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_cpu_cores("-1")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_cpu_cores("0")


def test_parse_memory():
    assert _parse_memory("auto") > 0
    assert _parse_memory(None) > 0
    assert _parse_memory("4.5") == 4.5
    assert _parse_memory(8) == 8.0

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_memory("bad_mem")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_memory("-2.0")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_memory("0")


# ===========================================================================
# 3. Session State Initialization Tests
# ===========================================================================

def test_build_initial_state_without_config_file():
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        cpu_cores=4,
        memory_gb=8.0,
        db_config_path="/tmp/non_existent.config",
        production_db=False,
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        output_path="/tmp/out.dat",
        dry_run=True,
    )
    assert state["target"] == "/tmp/my_app"
    assert state["db_type"] == "postgres"
    assert state["cpu_cores"] == 4
    assert state["memory_gb"] == 8.0
    assert state["production_db"] is False
    assert state["env"] == "staging"
    assert state["dry_run"] is True
    assert state["retry_count"] == 0
    assert "staging_db_config" not in state


def test_build_initial_state_with_valid_config(sample_ini_path):
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="postgres",
        cpu_cores=2,
        memory_gb=4.0,
        db_config_path=str(sample_ini_path),
        production_db=False,
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        output_path="/tmp/out.dat",
        dry_run=False,
    )
    assert "staging_db_config" in state
    stg_cfg = state["staging_db_config"]
    assert isinstance(stg_cfg, DBConfig)
    assert stg_cfg.host == "10.0.0.2"
    assert stg_cfg.port == 5432
    assert stg_cfg.database == "stg_db"
    assert state["database"] == "stg_db"
    assert state["dbname"] == "stg_db"


def test_build_initial_state_production_env(sample_ini_path):
    state = build_initial_state(
        target="/tmp/my_app",
        db_type="mysql",
        cpu_cores=4,
        memory_gb=16.0,
        db_config_path=str(sample_ini_path),
        production_db=True,
        log_file="/tmp/log.log",
        knob_path="/tmp/knobs",
        output_path="/tmp/out.dat",
        dry_run=False,
    )
    assert state["env"] == "production"
    assert state["production_db"] is True
    assert "production_db_config" in state
    prod_cfg = state["production_db_config"]
    assert prod_cfg.host == "127.0.0.1"
    assert prod_cfg.port == 3306
    assert prod_cfg.database == "prod_db"
    assert state["database"] == "prod_db"
    assert state["dbname"] == "prod_db"


def test_build_initial_state_strict_db_config_path_no_autodiscovery(tmp_path: Path):
    target_dir = tmp_path / "my_target_app"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_cfg = target_dir / "db.config"
    target_cfg.write_text(
        """[postgres]
host = 192.168.1.50
port = 5432
user = testuser
password = testpass
database = app_local_db
restart_type = docker
restart_target = stg_pg_app
""",
        encoding="utf-8",
    )

    custom_cfg = tmp_path / "custom.config"
    custom_cfg.write_text(
        """[postgres]
host = 10.0.0.99
port = 5432
user = customuser
password = custompass
database = custom_db
restart_type = docker
restart_target = stg_custom_app
""",
        encoding="utf-8",
    )

    # 1. Providing explicit custom_cfg does not bind target/db.config
    state_custom = build_initial_state(
        target=str(target_dir),
        db_type="postgres",
        cpu_cores=4,
        memory_gb=8.0,
        db_config_path=str(custom_cfg),
        production_db=False,
        log_file=str(tmp_path / "log.log"),
        knob_path=str(tmp_path / "knobs"),
        output_path=str(tmp_path / "out.json"),
        dry_run=False,
    )
    assert state_custom["db_config_path"] == str(custom_cfg)
    assert state_custom["database"] == "custom_db"
    assert state_custom["staging_db_config"].host == "10.0.0.99"

    # 2. Providing non-existent config does NOT auto-bind target/db.config
    absent_cfg = str(tmp_path / "absent.config")
    state_absent = build_initial_state(
        target=str(target_dir),
        db_type="postgres",
        cpu_cores=4,
        memory_gb=8.0,
        db_config_path=absent_cfg,
        production_db=False,
        log_file=str(tmp_path / "log.log"),
        knob_path=str(tmp_path / "knobs"),
        output_path=str(tmp_path / "out.json"),
        dry_run=False,
    )
    assert state_absent["db_config_path"] == absent_cfg
    assert state_absent["config_path"] == absent_cfg
    assert "staging_db_config" not in state_absent
    assert "database" not in state_absent

    # 3. Default "db.config" remains "db.config" and is not rewritten to target/db.config
    state_default = build_initial_state(
        target=str(target_dir),
        db_type="postgres",
        cpu_cores=4,
        memory_gb=8.0,
        db_config_path="db.config",
        production_db=False,
        log_file=str(tmp_path / "log.log"),
        knob_path=str(tmp_path / "knobs"),
        output_path=str(tmp_path / "out.json"),
        dry_run=False,
    )
    assert state_default["db_config_path"] == "db.config"
    assert state_default["config_path"] == "db.config"


# ===========================================================================
# 4. Helper Functions & Result Writing Tests
# ===========================================================================

def test_maybe_parse_dict_and_string():
    assert _maybe_parse({"a": 1}) == {"a": 1}
    assert _maybe_parse('{"status": "PASS"}') == {"status": "PASS"}
    assert _maybe_parse('```json\n{"status": "FAIL"}\n```') == {"status": "FAIL"}
    assert _maybe_parse("invalid string") == {}
    assert _maybe_parse(None) == {}


def test_log_event(tmp_path: Path):
    log_file = tmp_path / "test.log"
    _log_event("Test message 1", log_file=str(log_file), verbose=False)
    _log_event("Test message 2", log_file=str(log_file), verbose=False)

    content = log_file.read_text(encoding="utf-8")
    assert "Test message 1" in content
    assert "Test message 2" in content


def test_write_output_result(tmp_path: Path):
    out_file = tmp_path / "subdir" / "result.json"
    state = {
        "target": "/code/app",
        "db_type": "postgres",
        "cpu_cores": 4,
        "memory_gb": 8.0,
        "staging_validated": True,
        "knob_checker_output": {"status": "PASS"},
        "live_tuner_output": {"status": "APPLIED"},
    }
    _write_output_result(str(out_file), state)
    assert out_file.is_file()

    with open(out_file, encoding="utf-8") as f:
        data = json.load(f)

    assert data["target"] == "/code/app"
    assert data["db_type"] == "postgres"
    assert data["staging_validated"] is True
    assert data["knob_checker_output"] == {"status": "PASS"}
    assert data["live_tuner_output"] == {"status": "APPLIED"}


# ===========================================================================
# 5. Pipeline Execution and CLI Main Tests
# ===========================================================================

def test_run_pipeline_mocked(tmp_path: Path):
    target_dir = tmp_path / "target_app"
    target_dir.mkdir()
    out_file = tmp_path / "out" / "result.json"
    log_file = tmp_path / "logs" / "tuner.log"

    mock_event = MagicMock()
    mock_event.content.parts = [MagicMock(function_call=None, function_response=None, text="Completed")]
    mock_event.partial = False

    async def mock_run_async(*args, **kwargs):
        yield mock_event

    with patch("src.knob_tuner.main.Runner") as mock_runner_cls:
        mock_runner = MagicMock()
        mock_runner.run_async = mock_run_async
        mock_runner_cls.return_value = mock_runner

        res = asyncio.run(
            run_pipeline(
                target=str(target_dir),
                model="gemini-3.5-flash-lite",
                db_type="postgres",
                cpu_cores_arg=2,
                memory_arg=4.0,
                db_config="db.config",
                production_db=False,
                log_file=str(log_file),
                knob_path=str(tmp_path / "knobs"),
                output_path=str(out_file),
                dry_run=True,
                verbose=False,
            )
        )

        assert res["target"] == str(target_dir.resolve())
        assert res["db_type"] == "postgres"
        assert res["dry_run"] is True
        assert out_file.is_file()


def test_main_cli_invalid_target(capsys):
    with patch.object(sys, "argv", ["knob_tuner", "/path/that/does/not/exist/at/all"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "ERROR: target directory not found" in captured.err


def test_main_cli_success(tmp_path: Path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()

    mock_state = {
        "target": str(target_dir),
        "db_type": "postgres",
        "knob_checker_output": {"status": "PASS"},
        "live_tuner_output": {"status": "APPLIED"},
        "staging_validated": True,
    }

    with patch("src.knob_tuner.main.run_pipeline", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_state
        with patch.object(sys, "argv", ["knob_tuner", str(target_dir)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0


def test_main_cli_failure(tmp_path: Path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()

    mock_state = {
        "target": str(target_dir),
        "db_type": "postgres",
        "knob_checker_output": {
            "status": "FAIL",
            "issues": [
                {
                    "knob": "shared_buffers",
                    "description": "Database failed to restart with 100GB",
                    "suggestion": "Reduce shared_buffers to 4GB",
                }
            ],
        },
        "live_tuner_output": {"status": "SKIPPED"},
        "staging_validated": False,
    }

    with patch("src.knob_tuner.main.run_pipeline", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_state
        with patch.object(sys, "argv", ["knob_tuner", str(target_dir)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1


def test_main_cli_exception(tmp_path: Path, capsys):
    target_dir = tmp_path / "app"
    target_dir.mkdir()

    with patch("src.knob_tuner.main.run_pipeline", side_effect=RuntimeError("Connection exploded")):
        with patch.object(sys, "argv", ["knob_tuner", str(target_dir)]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1
            captured = capsys.readouterr()
            assert "Knob Tuner Pipeline FAILED" in captured.err
            assert "Connection exploded" in captured.err


# ===========================================================================
# 6. Process Lifecycle, Safety Handlers & Active Container Tracking Tests
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
    assert "cleanup_test_container_2" in ACTIVE_CONTAINERS

    with patch("src.knob_tuner.main.stop_staging_db") as mock_stop:
        _process_cleanup()
        mock_stop.assert_any_call("cleanup_test_container_1")
        mock_stop.assert_any_call("cleanup_test_container_2")

    unregister_active_container("cleanup_test_container_1")
    unregister_active_container("cleanup_test_container_2")


def test_signal_handler_triggers_cleanup_and_exit():
    import signal
    with patch("src.knob_tuner.main._process_cleanup") as mock_cleanup, patch("sys.exit") as mock_exit:
        _signal_handler(signal.SIGINT, None)
        mock_cleanup.assert_called_once()
        mock_exit.assert_called_once_with(128 + signal.SIGINT)


def test_run_pipeline_orphan_cleanup_called(tmp_path: Path):
    target_dir = tmp_path / "target_app"
    target_dir.mkdir()

    mock_event = MagicMock()
    mock_event.content.parts = [MagicMock(function_call=None, function_response=None, text="Done")]
    mock_event.partial = False

    async def mock_run_async(*args, **kwargs):
        yield mock_event

    with patch("src.knob_tuner.main.cleanup_orphan_containers", return_value=["stale_c1"]) as mock_cleanup:
        with patch("src.knob_tuner.main.Runner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner.run_async = mock_run_async
            mock_runner_cls.return_value = mock_runner

            asyncio.run(
                run_pipeline(
                    target=str(target_dir),
                    model="gemini-3.5-flash-lite",
                    db_type="postgres",
                    output_path=str(tmp_path / "res.json"),
                    log_file=str(tmp_path / "log.log"),
                    knob_path=str(tmp_path / "knobs"),
                    cleanup_orphans=True,
                )
            )

            mock_cleanup.assert_called_once()


def test_run_pipeline_orphan_cleanup_skipped(tmp_path: Path):
    target_dir = tmp_path / "target_app"
    target_dir.mkdir()

    mock_event = MagicMock()
    mock_event.content.parts = [MagicMock(function_call=None, function_response=None, text="Done")]
    mock_event.partial = False

    async def mock_run_async(*args, **kwargs):
        yield mock_event

    with patch("src.knob_tuner.main.cleanup_orphan_containers") as mock_cleanup:
        with patch("src.knob_tuner.main.Runner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner.run_async = mock_run_async
            mock_runner_cls.return_value = mock_runner

            asyncio.run(
                run_pipeline(
                    target=str(target_dir),
                    model="gemini-3.5-flash-lite",
                    db_type="postgres",
                    output_path=str(tmp_path / "res.json"),
                    log_file=str(tmp_path / "log.log"),
                    knob_path=str(tmp_path / "knobs"),
                    cleanup_orphans=False,
                )
            )

            mock_cleanup.assert_not_called()


def test_run_pipeline_exception_triggers_cleanup(tmp_path: Path):
    target_dir = tmp_path / "target_app"
    target_dir.mkdir()

    with patch("src.knob_tuner.main.Runner") as mock_runner_cls, patch("src.knob_tuner.main._process_cleanup") as mock_cleanup:
        mock_runner = MagicMock()
        mock_runner.run_async.side_effect = RuntimeError("Runner crashed")
        mock_runner_cls.return_value = mock_runner

        with pytest.raises(RuntimeError, match="Runner crashed"):
            asyncio.run(
                run_pipeline(
                    target=str(target_dir),
                    model="gemini-3.5-flash-lite",
                    db_type="postgres",
                    output_path=str(tmp_path / "res.json"),
                    log_file=str(tmp_path / "log.log"),
                    knob_path=str(tmp_path / "knobs"),
                    cleanup_orphans=False,
                )
            )

        mock_cleanup.assert_called_once()


def test_build_initial_state_registers_active_docker_container(sample_ini_path):
    with patch("src.knob_tuner.main.register_active_container") as mock_register:
        build_initial_state(
            target="/tmp/app",
            db_type="postgres",
            cpu_cores=2,
            memory_gb=4.0,
            db_config_path=str(sample_ini_path),
            production_db=False,
            log_file="/tmp/log.log",
            knob_path="/tmp/knobs",
            output_path="/tmp/res.json",
            dry_run=False,
        )
        mock_register.assert_called_with("stg_pg_container")

