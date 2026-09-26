"""Tests for the code_rewriter programmatic entry point (no live agents)."""

import asyncio
import json
import os
from unittest.mock import patch, MagicMock

import pytest

from src.code_rewriter.main import (
    _log_event,
    _maybe_parse,
    _write_output_result,
    run_pipeline,
)


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
    intent = {"optimization_targets": []}

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
            verbose=False,
            extra_initial_state={
                "intent_output": intent,
                "intent_extractor_output": intent,
            },
        )
        assert "target" in state
        assert os.path.exists(str(out_file))
        assert os.path.exists(str(log_file))

    asyncio.run(run_test())


def test_run_pipeline_requires_intent(tmp_path):
    async def run_test():
        with pytest.raises(RuntimeError, match="intent_output is required"):
            await run_pipeline(
                target=".",
                model="test",
                log_file=str(tmp_path / "log.txt"),
                output_path=str(tmp_path / "out.json"),
                verbose=False,
            )

    asyncio.run(run_test())
