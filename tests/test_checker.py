"""Tests for checker tools and schemas (no live LLM)."""

import os
import tempfile
from pathlib import Path

import pytest

from src.checker.models import CheckerIssue, CheckerOutput
from src.checker.tools import find_modified_files, read_file, read_original_file, list_sandbox


# ---------------------------------------------------------------------------
# test helpers
# ---------------------------------------------------------------------------

class MockState(dict):
    pass


class MockToolContext:
    def __init__(self, state=None):
        self.state = state if state is not None else MockState()


def _make_sandbox(files: dict) -> str:
    """Create a temp directory with given {relpath: content} files."""
    d = tempfile.mkdtemp()
    for rel, content in files.items():
        full = Path(d) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return d


# ---------------------------------------------------------------------------
# output schemas
# ---------------------------------------------------------------------------

def test_checker_issue_schema_validates():
    m = CheckerIssue.model_validate({
        "file": "db.py",
        "line": 42,
        "severity": "high",
        "category": "safety",
        "description": "SQL injection via string concat",
        "suggestion": "Use parameterized queries",
    })
    assert m.file == "db.py"
    assert m.line == 42
    assert m.severity == "high"
    assert m.category == "safety"


def test_checker_issue_defaults():
    m = CheckerIssue.model_validate({
        "file": "x.py",
        "severity": "low",
        "category": "correctness",
        "description": "off-by-one",
    })
    assert m.line == 0
    assert m.suggestion == ""


def test_checker_output_pass():
    m = CheckerOutput.model_validate({
        "status": "PASS",
        "issues": [],
        "summary": "All clear.",
    })
    assert m.status == "PASS"
    assert m.issues == []


def test_checker_output_fail():
    m = CheckerOutput.model_validate({
        "status": "FAIL",
        "issues": [
            {
                "file": "db.py",
                "line": 10,
                "severity": "critical",
                "category": "safety",
                "description": "SQL injection",
            }
        ],
        "summary": "Critical security issue found.",
    })
    assert m.status == "FAIL"
    assert len(m.issues) == 1
    assert m.issues[0].severity == "critical"


def test_checker_output_warn():
    m = CheckerOutput.model_validate({
        "status": "WARN",
        "issues": [
            {
                "file": "a.py",
                "line": 5,
                "severity": "medium",
                "category": "performance_regression",
                "description": "removed cache",
            }
        ],
        "summary": "One medium issue.",
    })
    assert m.status == "WARN"


# ---------------------------------------------------------------------------
# find_modified_files
# ---------------------------------------------------------------------------

def test_find_modified_no_tag():
    d = _make_sandbox({"a.py": "print(1)\n", "b.py": "x = 1\n"})
    ctx = MockToolContext({"sandbox": d})
    result = find_modified_files(ctx)
    assert "No modified files found" in result
    assert ctx.state["modified_files"] == []


def test_find_modified_with_tag():
    d = _make_sandbox({
        "a.py": "# ADCO_OPTIMIZED: abc123\nprint(1)\n",
        "b.py": "print(2)\n",
    })
    ctx = MockToolContext({"sandbox": d})
    result = find_modified_files(ctx)
    assert "Found 1 modified file" in result
    assert "a.py" in result
    assert ctx.state["modified_files"] == ["a.py"]


def test_find_modified_sql_tag():
    d = _make_sandbox({
        "query.sql": "-- ADCO_OPTIMIZED: abc\nSELECT 1;\n",
    })
    ctx = MockToolContext({"sandbox": d})
    result = find_modified_files(ctx)
    assert "query.sql" in result
    assert ctx.state["modified_files"] == ["query.sql"]


def test_find_modified_multiple():
    d = _make_sandbox({
        "a.py": "# ADCO_OPTIMIZED: x\nprint(1)",
        "b.py": "# ADCO_OPTIMIZED: y\nprint(2)",
        "c.py": "print(3)",
    })
    ctx = MockToolContext({"sandbox": d})
    result = find_modified_files(ctx)
    assert "Found 2 modified file" in result
    assert ctx.state["modified_files"] == ["a.py", "b.py"]


def test_find_modified_nested():
    d = _make_sandbox({
        "sub/a.py": "# ADCO_OPTIMIZED: x\nprint(1)",
        "sub/dep/b.py": "# ADCO_OPTIMIZED: x\nprint(2)",
    })
    ctx = MockToolContext({"sandbox": d})
    result = find_modified_files(ctx)
    assert "sub/a.py" in result
    assert "sub/dep/b.py" in result
    assert ctx.state["modified_files"] == ["sub/a.py", "sub/dep/b.py"]


def test_find_modified_no_sandbox():
    ctx = MockToolContext({"sandbox": ""})
    result = find_modified_files(ctx)
    assert "ERROR" in result


def test_find_modified_bad_sandbox():
    ctx = MockToolContext({"sandbox": "/nonexistent/path/xyz"})
    result = find_modified_files(ctx)
    assert "ERROR" in result


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

def test_read_file_ok():
    d = _make_sandbox({"main.py": "print('hello')\nprint('world')\n"})
    ctx = MockToolContext({"sandbox": d})
    result = read_file("main.py", ctx)
    assert "=== main.py" in result
    assert "1| print('hello')" in result
    assert "2| print('world')" in result


def test_read_file_not_found():
    d = _make_sandbox({"a.py": "x=1"})
    ctx = MockToolContext({"sandbox": d})
    result = read_file("missing.py", ctx)
    assert "ERROR" in result
    assert "not found" in result or "file not found" in result


def test_read_file_path_traversal():
    d = _make_sandbox({"a.py": "x=1"})
    ctx = MockToolContext({"sandbox": d})
    result = read_file("../etc/passwd", ctx)
    assert "ERROR" in result
    assert "traversal" in result.lower()


def test_read_file_no_sandbox():
    ctx = MockToolContext({"sandbox": ""})
    result = read_file("a.py", ctx)
    assert "ERROR" in result


# ---------------------------------------------------------------------------
# list_sandbox
# ---------------------------------------------------------------------------

def test_list_sandbox_basic():
    d = _make_sandbox({"a.py": "# ADCO_OPTIMIZED: x\npass", "b.py": "pass"})
    ctx = MockToolContext({"sandbox": d, "modified_files": ["a.py"]})
    result = list_sandbox(ctx)
    assert "a.py *" in result
    assert "b.py" in result


def test_list_sandbox_no_sandbox():
    ctx = MockToolContext({"sandbox": ""})
    result = list_sandbox(ctx)
    assert "ERROR" in result


# ---------------------------------------------------------------------------
# read_original_file
# ---------------------------------------------------------------------------

def test_read_original_file_ok():
    d = _make_sandbox({"main.py": "def greet():\n    return 'hi'\n"})
    ctx = MockToolContext({"sandbox": "/tmp/irrelevant", "original": d})
    result = read_original_file("main.py", ctx)
    assert "=== [ORIGINAL] main.py" in result
    assert "1| def greet():" in result
    assert "2|     return 'hi'" in result


def test_read_original_file_not_found():
    d = _make_sandbox({"a.py": "x=1"})
    ctx = MockToolContext({"sandbox": "/tmp/irrelevant", "original": d})
    result = read_original_file("b.py", ctx)
    assert "INFO" in result
    assert "not found" in result


def test_read_original_file_path_traversal():
    d = _make_sandbox({"a.py": "x=1"})
    ctx = MockToolContext({"sandbox": "/tmp/irrelevant", "original": d})
    result = read_original_file("../etc/passwd", ctx)
    assert "ERROR" in result
    assert "traversal" in result.lower()


def test_read_original_file_no_original():
    ctx = MockToolContext({"sandbox": "/tmp/irrelevant"})
    result = read_original_file("a.py", ctx)
    assert "ERROR" in result
    assert "original path not set" in result


# ---------------------------------------------------------------------------
# agent factory
# ---------------------------------------------------------------------------

def test_create_checker_agent():
    from src.checker.agent import create_checker_agent
    agent = create_checker_agent("gemini-2.5-flash")
    assert agent.name == "adco_checker"
    assert agent.output_key == "checker_output"
    assert agent.output_schema == CheckerOutput
    assert len(agent.tools) == 4
