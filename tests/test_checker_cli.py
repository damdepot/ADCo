import argparse
import json
import os
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from src.code_checker.main import (
    build_parser,
    _log_event,
    _maybe_parse,
    _write_output_result,
    run_checker,
    main,
)

def test_maybe_parse():
    # Test valid JSON string
    assert _maybe_parse('{"status": "PASS"}') == {"status": "PASS"}
    
    # Test markdown fenced JSON
    assert _maybe_parse('```json\n{"status": "FAIL"}\n```') == {"status": "FAIL"}
    
    # Test invalid JSON string
    assert _maybe_parse('invalid') == {}
    
    # Test dict
    assert _maybe_parse({"key": "value"}) == {"key": "value"}
    
    # Test model dump
    class DummyModel:
        def model_dump(self):
            return {"dumped": True}
    assert _maybe_parse(DummyModel()) == {"dumped": True}
    
    # Test other
    assert _maybe_parse(123) == {}


def test_log_event(tmp_path, capsys):
    log_file = tmp_path / "test.log"
    
    # Test file logging
    _log_event("test message", log_file=str(log_file))
    assert log_file.exists()
    assert "test message" in log_file.read_text()
    
    # Test stdout logging
    _log_event("verbose message", log_file=None, verbose=True)
    captured = capsys.readouterr()
    assert "verbose message" in captured.out


def test_build_parser():
    parser = build_parser()
    args = parser.parse_args(["/tmp/sandbox"])
    assert args.sandbox_dir == "/tmp/sandbox"
    assert args.original == ""
    assert args.model == "gemini-3.5-flash-lite"
    assert args.log_file == "logs/code_checker.log"
    assert args.output_path == "out/code_checker/result.json"
    assert args.verbose is False


def test_write_output_result(tmp_path):
    output_path = tmp_path / "result.json"
    state = {
        "sandbox": "/tmp/sandbox",
        "original": "/tmp/original",
        "checker_output": {
            "status": "PASS",
            "summary": "Looks good",
            "issues": []
        }
    }
    
    _write_output_result(str(output_path), state, model="gemini-4-flash")
    
    assert output_path.exists()
    data = json.loads(output_path.read_text())
    
    assert data["sandbox"] == "/tmp/sandbox"
    assert data["original"] == "/tmp/original"
    assert data["model"] == "gemini-4-flash"
    assert data["status"] == "PASS"
    assert data["issues_count"] == 0
    assert data["issues"] == []
    assert data["summary"] == "Looks good"
    assert "timestamp" in data
    assert data["checker_output"] == state["checker_output"]


@patch("src.code_checker.main.Runner")
@patch("src.code_checker.main.create_checker_agent")
def test_run_checker(mock_create_agent, mock_runner_class, tmp_path):
    import asyncio
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    original = tmp_path / "original"
    original.mkdir()
    
    log_file = tmp_path / "test.log"
    output_path = tmp_path / "result.json"
    
    mock_agent = MagicMock()
    mock_create_agent.return_value = mock_agent
    
    mock_runner = MagicMock()
    mock_runner_class.return_value = mock_runner
    
    async def mock_run_async(*args, **kwargs):
        class MockEvent:
            def __init__(self, text):
                class MockPart:
                    def __init__(self, t):
                        self.text = t
                        self.function_call = None
                        self.function_response = None
                class MockContent:
                    def __init__(self, t):
                        self.parts = [MockPart(t)]
                self.content = MockContent(text)
                self.partial = False
        yield MockEvent("Checking...")
        
    mock_runner.run_async = mock_run_async
    
    # We patch InMemorySessionService.get_session to return a session with state
    with patch("src.code_checker.main.InMemorySessionService") as mock_session_service_class:
        mock_session_service = MagicMock()
        mock_session_service_class.return_value = mock_session_service
        
        # Also need to patch async methods
        mock_session_service.create_session = AsyncMock()
        
        mock_session = MagicMock()
        mock_session.state = {
            "sandbox": str(sandbox),
            "original": str(original),
            "checker_output": {"status": "FAIL", "issues": []}
        }
        mock_session_service.get_session = AsyncMock(return_value=mock_session)
    
        state = asyncio.run(run_checker(
            sandbox=str(sandbox),
            original=str(original),
            log_file=str(log_file),
            output_path=str(output_path),
            verbose=True
        ))
        
        assert state["sandbox"] == str(sandbox)
        assert state["checker_output"]["status"] == "FAIL"
        
        assert log_file.exists()
        assert output_path.exists()


@patch("src.code_checker.main.run_checker")
def test_main_success(mock_run_checker, capsys, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    
    # Mock return value of run_checker (AsyncMock is needed because run_checker is async, 
    # but asyncio.run is called on it, so we need run_checker to return a coroutine)
    async def mock_run(*args, **kwargs):
        return {
            "checker_output": {
                "status": "PASS",
                "issues": [],
                "summary": "All tests passed"
            }
        }
    
    mock_run_checker.side_effect = mock_run
    
    with patch("sys.argv", ["main.py", str(sandbox)]):
        with pytest.raises(SystemExit) as exc_info:
            main()
            
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "All tests passed" in captured.out


@patch("src.code_checker.main.run_checker")
def test_main_fail(mock_run_checker, capsys, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    
    async def mock_run(*args, **kwargs):
        return {
            "checker_output": {
                "status": "FAIL",
                "issues": [{"severity": "HIGH", "category": "SEC", "file": "test.py", "line": 10, "description": "SQL Injection"}],
                "summary": "Found issues"
            }
        }
    
    mock_run_checker.side_effect = mock_run
    
    with patch("sys.argv", ["main.py", str(sandbox)]):
        with pytest.raises(SystemExit) as exc_info:
            main()
            
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "SQL Injection" in captured.out
