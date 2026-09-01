import argparse
import asyncio
import json
import os
from unittest.mock import patch, MagicMock

import pytest

from src.code_rewriter.main import (
    build_parser,
    _log_event,
    _maybe_parse,
    _write_output_result,
    run_pipeline,
    main
)


def test_build_parser():
    parser = build_parser()
    assert isinstance(parser, argparse.ArgumentParser)
    args = parser.parse_args(["target_dir"])
    assert args.target == "target_dir"
    assert args.model == "gemini-3.5-flash-lite"
    assert args.log_file == "logs/code_rewriter.log"
    assert args.output_path == "out/code_rewriter/result.json"
    assert args.sandbox_dir is None
    assert args.verbose is False

    args = parser.parse_args(["target_dir", "--model", "test-model", "-v", "--sandbox-dir", "sbx"])
    assert args.model == "test-model"
    assert args.sandbox_dir == "sbx"
    assert args.verbose is True


def test_maybe_parse():
    # String JSON
    assert _maybe_parse('{"status": "PASS"}') == {"status": "PASS"}
    # String JSON with markdown fences
    assert _maybe_parse('```json\n{"status": "PASS"}\n```') == {"status": "PASS"}
    # Dict
    assert _maybe_parse({"status": "PASS"}) == {"status": "PASS"}
    
    # Pydantic-like object
    class DummyModel:
        def model_dump(self):
            return {"a": 1}
    assert _maybe_parse(DummyModel()) == {"a": 1}

    # Invalid string
    assert _maybe_parse("invalid") == {}
    
    # Int
    assert _maybe_parse(1) == {}


def test_log_event(tmp_path):
    log_file = tmp_path / "test.log"
    _log_event("test message", log_file=str(log_file), verbose=False)
    
    assert log_file.exists()
    content = log_file.read_text()
    assert "test message" in content
    # check timestamp format like [2023-01-01 12:00:00]
    assert "[" in content and "]" in content


def test_write_output_result(tmp_path):
    output_path = tmp_path / "result.json"
    state = {
        "target": "target_dir",
        "sandbox": "sandbox_dir",
        "verifier_output": '{"status": "PASS"}',
        "modified_files": ["a.py"],
        "scan_result": {"scanned": True},
    }
    
    _write_output_result(str(output_path), state, model="test-model")
    
    assert output_path.exists()
    data = json.loads(output_path.read_text())
    
    assert data["target"] == "target_dir"
    assert data["model"] == "test-model"
    assert data["sandbox"] == "sandbox_dir"
    assert data["status"] == "PASS"
    assert data["modified_files"] == ["a.py"]
    assert data["outputs"]["scan_result"] == {"scanned": True}
    assert data["outputs"]["verifier_output"] == {"status": "PASS"}


@patch("src.code_rewriter.main.Runner")
@patch("src.code_rewriter.main.create_root_agent")
def test_run_pipeline(mock_create_root_agent, mock_runner_class, tmp_path):
    log_file = tmp_path / "log.txt"
    out_file = tmp_path / "out.json"
    
    mock_runner_instance = MagicMock()
    mock_runner_class.return_value = mock_runner_instance
    
    # Mock run_async to yield nothing
    async def mock_run_async(*args, **kwargs):
        if False:
            yield
    
    mock_runner_instance.run_async = mock_run_async
    
    async def run_test():
        state = await run_pipeline(
            target=".", 
            model="test",
            log_file=str(log_file),
            output_path=str(out_file),
            sandbox_dir=None,
            verbose=False
        )
        assert "target" in state
        assert os.path.exists(str(out_file))
        assert os.path.exists(str(log_file))

    asyncio.run(run_test())


@patch("src.code_rewriter.main.run_pipeline")
def test_main_pass(mock_run_pipeline, monkeypatch, capsys, tmp_path):
    mock_run_pipeline.return_value = {
        "verifier_output": {"status": "PASS"},
        "sandbox": "sandbox_dir",
        "modified_files": ["a.py"]
    }
    
    d = tmp_path / "test_dir"
    d.mkdir()
    monkeypatch.setattr("sys.argv", ["main.py", str(d)])
    
    with pytest.raises(SystemExit) as e:
        main()
        
    assert e.value.code == 0
    captured = capsys.readouterr()
    assert "=== Pipeline PASSED ===" in captured.out


@patch("src.code_rewriter.main.run_pipeline")
def test_main_fail(mock_run_pipeline, monkeypatch, capsys, tmp_path):
    mock_run_pipeline.return_value = {
        "verifier_output": {"status": "FAIL", "reason": "test"},
        "sandbox": "sandbox_dir",
        "modified_files": []
    }
    
    d = tmp_path / "test_dir"
    d.mkdir()
    monkeypatch.setattr("sys.argv", ["main.py", str(d)])
    
    with pytest.raises(SystemExit) as e:
        main()
        
    assert e.value.code == 1
    captured = capsys.readouterr()
    assert "=== Pipeline FAILED ===" in captured.out


def test_main_invalid_target(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["main.py", "non_existent_dir"])
    
    with pytest.raises(SystemExit) as e:
        main()
        
    assert e.value.code == 2
