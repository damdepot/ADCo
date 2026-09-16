"""Tests for the new intent_analyzer module."""
import os
import pytest
from unittest.mock import MagicMock

from src.intent_analyzer.agent import create_intent_analyzer_agent, create_root_agent
from src.intent_analyzer.sub_agents.file_selector.agent import create_file_selector_agent
from src.intent_analyzer.sub_agents.intent_extractor.agent import create_intent_extractor_agent
from src.intent_analyzer.sub_agents.intent_extractor.models import (
    IntentExtractorOutput,
    OptimizationTarget,
    WorkloadPattern,
)
from src.intent_analyzer.tools.scanner import scan_directory, scan_codebase


def test_agent_creation():
    agent = create_intent_analyzer_agent("gemini-3.5-flash")
    assert agent.name == "intent_analyzer"
    assert agent.model == "gemini-3.5-flash"
    assert len(agent.tools) == 3

    root_agent = create_root_agent()
    assert root_agent.name == "intent_analyzer"
    assert root_agent.model == "gemini-3.5-flash"


def test_subagent_creation():
    fs_agent = create_file_selector_agent("gemini-3.5-flash")
    assert fs_agent.name == "file_selector"

    ie_agent = create_intent_extractor_agent("gemini-3.5-flash")
    assert ie_agent.name == "intent_extractor"


def test_models_serialization():
    workload = WorkloadPattern(
        query_types=["SELECT", "INSERT", "UPDATE"],
        orm_detected="SQLAlchemy",
        transaction_pattern="Explicit commit/rollback",
        estimated_read_write_ratio="80% Read / 20% Write (Read-Heavy)",
        notable_patterns=["N+1 query loops present"],
    )
    target = OptimizationTarget(file="models.py", description="Batch queries with executemany")
    intent = IntentExtractorOutput(
        connection="connection pool",
        queries="SELECT * FROM users",
        transactions="Explicit",
        n_plus_one="Loop in get_items",
        concurrency="Async",
        orm="SQLAlchemy",
        workload=workload,
        optimization_targets=[target],
        notes="None",
    )

    data = intent.model_dump()
    assert data["workload"]["orm_detected"] == "SQLAlchemy"
    assert "SELECT" in data["workload"]["query_types"]
    assert len(data["optimization_targets"]) == 1
    assert data["optimization_targets"][0]["file"] == "models.py"


def test_scanner_tool(tmp_path):
    d = tmp_path / "test_app"
    d.mkdir()
    (d / "main.py").write_text("print('hello')")
    (d / "db.py").write_text("import sqlite3")

    files = scan_directory(str(d))
    assert "main.py" in files
    assert "db.py" in files

    # Test tool context
    ctx = MagicMock()
    ctx.state = {"target": str(d)}
    res = scan_codebase(ctx)
    assert "Scanned 2 files" in res
    assert ctx.state["file_list"] == ["db.py", "main.py"]
