import argparse
import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from src.adco.agent import create_orchestrator_agent
from src.adco.main import (
    DEFAULT_MODEL,
    build_parser,
    main,
    run_pipeline,
)


def test_build_parser():
    parser = build_parser()
    assert isinstance(parser, argparse.ArgumentParser)

    args = parser.parse_args(["my_target", "--db-name", "test_db"])
    assert args.target == "my_target"
    assert args.db_name == "test_db"
    assert args.model == DEFAULT_MODEL
    assert args.db_type == "postgres"
    assert args.db_config == "db.config"
    assert args.cpu_cores == "auto"
    assert args.memory == "auto"
    assert args.log_file == "logs/adco.log"
    assert args.output_path == "out/adco/result.json"
    assert args.intent_output == "out/adco/intent_result.json"
    assert args.rewriter_output == "out/adco/rewriter_result.json"
    assert args.tuner_output == "out/adco/knob_result.json"
    assert args.knob_path == "out/adco/knobs"
    assert args.production_db is False
    assert args.dry_run is False
    assert args.verbose is False
    assert args.mode == "all"
    assert args.buffer_time == 0.0


def test_build_parser_custom_options():
    parser = build_parser()
    args = parser.parse_args([
        "my_target",
        "--db-name", "my_db",
        "--db-type", "mysql",
        "--model", "gemini-test",
        "--cpu-cores", "4",
        "--memory", "8",
        "--production-db",
        "--dry-run",
        "-v",
        "--mode", "tune-only",
        "--buffer-time", "2.5",
    ])
    assert args.target == "my_target"
    assert args.db_name == "my_db"
    assert args.db_type == "mysql"
    assert args.model == "gemini-test"
    assert args.cpu_cores == "4"
    assert args.memory == "8"
    assert args.production_db is True
    assert args.dry_run is True
    assert args.verbose is True
    assert args.mode == "tune-only"
    assert args.buffer_time == 2.5



def test_create_orchestrator_agent_buffer_time():
    import asyncio
    
    agent_default = create_orchestrator_agent()
    assert agent_default.before_model_callback is None
    
    agent_with_buffer = create_orchestrator_agent(buffer_time=1.5)
    assert callable(agent_with_buffer.before_model_callback)
    assert asyncio.iscoroutinefunction(agent_with_buffer.before_model_callback)

def test_create_orchestrator_agent():
    agent = create_orchestrator_agent()
    assert agent.name == "adco_orchestrator"
    assert agent.model == "gemini-3.5-flash-lite"
    assert len(agent.tools) == 3


@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.rewriter_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.tuner_pipeline", new_callable=AsyncMock)
def test_run_pipeline_success_with_tuning(mock_tuner, mock_rewriter, mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    log_file = tmp_path / "adco.log"
    out_file = tmp_path / "result.json"
    intent_out = tmp_path / "intent.json"
    rewriter_out = tmp_path / "rewriter.json"
    tuner_out = tmp_path / "tuner.json"

    intent_out.write_text(json.dumps({"intent_output": {"queries": "SELECT * FROM users"}}))
    rewriter_out.write_text(json.dumps({"status": "PASS", "sandbox": str(sandbox_dir)}))
    tuner_out.write_text(json.dumps({"staging_validated": True}))

    mock_intent.return_value = {
        "intent_output": {
            "queries": "SELECT * FROM users JOIN orders",
            "orm": "Peewee",
            "optimization_targets": [{"file": "main.py", "description": "Optimize JOIN query"}],
            "workload": {
                "query_types": ["SELECT", "JOIN"],
                "orm_detected": "Peewee",
            },
        },
        "workload_info": {
            "query_types": ["SELECT", "JOIN"],
            "orm_detected": "Peewee",
        },
    }

    mock_rewriter.return_value = {
        "verifier_output": {"status": "PASS"},
        "sandbox": str(sandbox_dir),
    }
    mock_tuner.return_value = {
        "staging_validated": True,
    }

    res = asyncio.run(
        run_pipeline(
            target=str(target_dir),
            model="gemini-3.5-flash-lite",
            log_file=str(log_file),
            output_path=str(out_file),
            intent_output_path=str(intent_out),
            rewriter_output=str(rewriter_out),
            db_name="testdb",
            db_type="postgres",
            tuner_output=str(tuner_out),
        )
    )

    assert mock_intent.called
    assert mock_rewriter.called
    assert mock_tuner.called

    # Verify tuner received sandbox path and workload_info
    tuner_call_kwargs = mock_tuner.call_args.kwargs
    assert tuner_call_kwargs["target"] == str(sandbox_dir)
    assert "workload_info" in tuner_call_kwargs["extra_initial_state"]
    assert "SELECT" in tuner_call_kwargs["extra_initial_state"]["workload_info"]["query_types"]

    assert os.path.exists(out_file)
    assert res["target"] == str(target_dir.resolve())
    assert res["sandbox"] == str(sandbox_dir)
    assert "intent_analyzer" in res
    assert "rewriter" in res
    assert "knob_tuner" in res


@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.rewriter_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.tuner_pipeline", new_callable=AsyncMock)
def test_run_pipeline_tune_only(mock_tuner, mock_rewriter, mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()

    log_file = tmp_path / "adco.log"
    out_file = tmp_path / "result.json"

    mock_intent.return_value = {
        "workload_info": {"query_types": ["SELECT"]}
    }
    mock_tuner.return_value = {"staging_validated": True}

    res = asyncio.run(
        run_pipeline(
            target=str(target_dir),
            mode="tune-only",
            log_file=str(log_file),
            output_path=str(out_file),
            db_name="testdb",
        )
    )

    assert mock_intent.called
    assert not mock_rewriter.called
    assert mock_tuner.called

    tuner_call_kwargs = mock_tuner.call_args.kwargs
    assert tuner_call_kwargs["target"] == str(target_dir.resolve())

    assert os.path.exists(out_file)
    assert res["mode"] == "tune-only"
    assert res["sandbox"] == str(target_dir.resolve())


@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.rewriter_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.tuner_pipeline", new_callable=AsyncMock)
def test_run_pipeline_rewrite_only(mock_tuner, mock_rewriter, mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    log_file = tmp_path / "adco.log"
    out_file = tmp_path / "result.json"
    intent_out = tmp_path / "intent.json"
    rewriter_out = tmp_path / "rewriter.json"

    intent_out.write_text(json.dumps({"intent_output": {"optimization_targets": [{"file": "main.py"}]}}))
    rewriter_out.write_text(json.dumps({"status": "PASS", "sandbox": str(sandbox_dir)}))

    mock_intent.return_value = {
        "intent_output": {
            "optimization_targets": [{"file": "main.py", "description": "Optimize query"}]
        }
    }
    mock_rewriter.return_value = {
        "verifier_output": {"status": "PASS"},
        "sandbox": str(sandbox_dir),
    }

    res = asyncio.run(
        run_pipeline(
            target=str(target_dir),
            model="gemini-3.5-flash-lite",
            mode="rewrite-only",
            log_file=str(log_file),
            output_path=str(out_file),
            intent_output_path=str(intent_out),
            rewriter_output=str(rewriter_out),
            db_name="testdb",
        )
    )

    assert mock_intent.called
    assert mock_rewriter.called
    assert not mock_tuner.called  # Tuning was skipped!

    assert os.path.exists(out_file)
    assert res["mode"] == "rewrite-only"
    assert res["sandbox"] == str(sandbox_dir)
    assert "rewriter" in res


@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
@patch("src.adco.main.rewriter_pipeline", new_callable=AsyncMock)
def test_run_pipeline_rewriter_fail_fast(mock_rewriter, mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()

    mock_intent.return_value = {
        "intent_output": {
            "optimization_targets": [{"file": "main.py", "description": "Optimize query"}]
        }
    }
    mock_rewriter.return_value = {
        "verifier_output": {
            "status": "FAIL",
            "category": "syntax_error",
            "reason": "Invalid Python syntax",
        }
    }

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(
            run_pipeline(
                target=str(target_dir),
                db_name="testdb",
            )
        )

    assert "Code rewriter FAILED" in str(exc_info.value)


@patch("src.adco.main.intent_analyzer_pipeline", new_callable=AsyncMock)
def test_run_pipeline_intent_fail_fast(mock_intent, tmp_path):
    target_dir = tmp_path / "app"
    target_dir.mkdir()

    mock_intent.return_value = {"intent_output": {}}

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(
            run_pipeline(
                target=str(target_dir),
                db_name="testdb",
            )
        )

    assert "Intent analyzer returned no output or no optimization_targets" in str(exc_info.value)


@patch("src.adco.main.run_pipeline", new_callable=AsyncMock)
def test_main_success(mock_run, monkeypatch, capsys, tmp_path):
    d = tmp_path / "target_app"
    d.mkdir()

    monkeypatch.setattr("sys.argv", ["adco", str(d), "--mode", "rewrite-only"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "=== ADCo Pipeline COMPLETED ===" in captured.out


def test_main_invalid_target(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["adco", "nonexistent_dir_123", "--db-name", "mydb"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "ERROR: target is not a directory" in captured.err
