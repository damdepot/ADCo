"""Tests for rewriter sub-agent tools and schemas (no live agents)."""

import os
import tempfile
from pathlib import Path

import pytest


class MockState(dict):
    pass


class MockToolContext:
    def __init__(self, state=None):
        self.state = state if state is not None else MockState()


# ---------------------------------------------------------------------------
# output schemas (pydantic)
# ---------------------------------------------------------------------------

def test_file_selector_output_schema_validates():
    from src.rewriter.sub_agents.file_selector.models import FileSelectorOutput
    m = FileSelectorOutput.model_validate({"files": ["a.py", "b.py"], "entry_point": "main.py"})
    assert m.files == ["a.py", "b.py"]
    assert m.entry_point == "main.py"


def test_intent_extractor_output_schema_validates():
    from src.rewriter.sub_agents.intent_extractor.models import IntentExtractorOutput
    data = {
        "connection": "pool", "queries": "crud", "transactions": "manual",
        "n_plus_one": "yes: loop in loader", "concurrency": "sequential",
        "orm": "raw sql",
        "optimization_targets": [{"file": "loader.py", "description": "batch inserts"}],
        "notes": "n/a",
    }
    m = IntentExtractorOutput.model_validate(data)
    assert m.optimization_targets[0].file == "loader.py"
    assert m.optimization_targets[0].description == "batch inserts"


def test_code_optimizer_output_schema_validates():
    from src.rewriter.sub_agents.code_optimizer.models import CodeOptimizerOutput
    m = CodeOptimizerOutput.model_validate({"modified_files": ["a.py"], "summary": "ok"})
    assert m.modified_files == ["a.py"]
    assert m.summary == "ok"


def test_verifier_output_schema_validates_pass_and_fail():
    from src.rewriter.sub_agents.verifier.models import VerifierOutput
    assert VerifierOutput.model_validate({"status": "PASS", "category": "NONE", "reason": "ok", "detail": ""}).status == "PASS"
    assert VerifierOutput.model_validate({"status": "FAIL", "category": "name_error", "reason": "boom", "detail": "x"}).status == "FAIL"


def test_verifier_output_schema_rejects_invalid_status():
    from src.rewriter.sub_agents.verifier.models import VerifierOutput
    with pytest.raises(Exception):
        VerifierOutput.model_validate({"status": "MAYBE"})


# ---------------------------------------------------------------------------
# code_optimizer.tools
# ---------------------------------------------------------------------------

from src.rewriter.sub_agents.code_optimizer.tools import (
    write_file as co_write_file,
    read_file as co_read_file,
    list_sandbox as co_list_sandbox,
    get_optimization_context as co_get_optimization_context,
)


def test_codeopt_write_file_writes_content_and_records_modified():
    with tempfile.TemporaryDirectory() as sandbox:
        tc = MockToolContext({"sandbox": sandbox})

        result = co_write_file("app.py", "print('hello')", tc)

        content = Path(os.path.join(sandbox, "app.py")).read_text()
        sandbox_id = os.path.basename(sandbox)
        assert content == f"# ADCO_OPTIMIZED: {sandbox_id}\nprint('hello')"
        assert "OK: wrote" in result
        assert tc.state["modified_files"] == ["app.py"]


def test_codeopt_write_file_records_multiple_unique_paths():
    with tempfile.TemporaryDirectory() as sandbox:
        tc = MockToolContext({"sandbox": sandbox})

        co_write_file("a.py", "x=1", tc)
        co_write_file("b.py", "y=2", tc)
        co_write_file("a.py", "x=3", tc)

        assert tc.state["modified_files"] == ["a.py", "b.py"]


def test_codeopt_write_file_rejects_empty_path():
    tc = MockToolContext({"sandbox": "/tmp"})

    result = co_write_file("", "bad", tc)

    assert "ERROR" in result
    assert "modified_files" not in tc.state


def test_codeopt_write_file_rejects_syntax_error():
    with tempfile.TemporaryDirectory() as sandbox:
        tc = MockToolContext({"sandbox": sandbox})

        result = co_write_file("bad.py", "import loggingfrom pprint import x\n", tc)

        assert "ERROR" in result
        assert "SyntaxError" in result
        assert not os.path.isfile(os.path.join(sandbox, "bad.py"))
        assert "modified_files" not in tc.state


def test_codeopt_write_file_accepts_valid_python():
    with tempfile.TemporaryDirectory() as sandbox:
        tc = MockToolContext({"sandbox": sandbox})

        result = co_write_file("good.py", "import os\nprint('hello')\n", tc)

        assert "OK: wrote" in result
        assert tc.state["modified_files"] == ["good.py"]


def test_codeopt_write_file_rejects_identical_content():
    with tempfile.TemporaryDirectory() as sandbox:
        original = "import os\nprint('hello')\n"
        Path(os.path.join(sandbox, "good.py")).write_text(original)
        tc = MockToolContext({"sandbox": sandbox})

        result = co_write_file("good.py", original, tc)

        assert "ERROR" in result
        assert "identical" in result
        assert "modified_files" not in tc.state


def test_codeopt_write_file_accepts_modified_content():
    with tempfile.TemporaryDirectory() as sandbox:
        original = "import os\nprint('hello')\n"
        Path(os.path.join(sandbox, "good.py")).write_text(original)
        tc = MockToolContext({"sandbox": sandbox})

        result = co_write_file("good.py", "import os\nprint('optimized')\n", tc)

        assert "OK: wrote" in result
        assert tc.state["modified_files"] == ["good.py"]


def test_codeopt_write_file_skips_syntax_check_for_non_python():
    with tempfile.TemporaryDirectory() as sandbox:
        tc = MockToolContext({"sandbox": sandbox})

        result = co_write_file("data.json", '{"key": "value"}', tc)

        assert "OK: wrote" in result
        assert tc.state["modified_files"] == ["data.json"]


def test_codeopt_write_file_adds_adco_tag_to_python():
    with tempfile.TemporaryDirectory() as sandbox:
        tc = MockToolContext({"sandbox": sandbox})
        sandbox_id = os.path.basename(sandbox)

        co_write_file("app.py", "x = 1\n", tc)

        content = Path(os.path.join(sandbox, "app.py")).read_text()
        assert content == f"# ADCO_OPTIMIZED: {sandbox_id}\nx = 1\n"


def test_codeopt_write_file_adds_adco_tag_to_sql():
    with tempfile.TemporaryDirectory() as sandbox:
        tc = MockToolContext({"sandbox": sandbox})
        sandbox_id = os.path.basename(sandbox)

        co_write_file("query.sql", "SELECT 1;\n", tc)

        content = Path(os.path.join(sandbox, "query.sql")).read_text()
        assert content == f"-- ADCO_OPTIMIZED: {sandbox_id}\nSELECT 1;\n"


def test_codeopt_write_file_replaces_existing_tag():
    with tempfile.TemporaryDirectory() as sandbox:
        tc = MockToolContext({"sandbox": sandbox})
        sandbox_id = os.path.basename(sandbox)
        old_content = "# ADCO_OPTIMIZED: old-id\nx = 1\n"
        Path(os.path.join(sandbox, "app.py")).write_text(old_content)

        co_write_file("app.py", "x = 2\n", tc)

        content = Path(os.path.join(sandbox, "app.py")).read_text()
        assert content == f"# ADCO_OPTIMIZED: {sandbox_id}\nx = 2\n"


def test_codeopt_write_file_no_tag_for_unsupported_extensions():
    with tempfile.TemporaryDirectory() as sandbox:
        tc = MockToolContext({"sandbox": sandbox})

        co_write_file("data.json", '{"key": "value"}', tc)

        content = Path(os.path.join(sandbox, "data.json")).read_text()
        assert content == '{"key": "value"}'


def test_codeopt_identity_check_ignores_tag():
    with tempfile.TemporaryDirectory() as sandbox:
        sandbox_id = os.path.basename(sandbox)
        tagged = f"# ADCO_OPTIMIZED: {sandbox_id}\nx = 1\n"
        Path(os.path.join(sandbox, "app.py")).write_text(tagged)
        tc = MockToolContext({"sandbox": sandbox})

        result = co_write_file("app.py", "x = 1\n", tc)

        assert "identical" in result
        assert "ERROR" in result


def test_codeopt_read_file_reads_content():
    with tempfile.TemporaryDirectory() as sandbox:
        Path(os.path.join(sandbox, "data.txt")).write_text("hello world")
        tc = MockToolContext({"sandbox": sandbox})

        result = co_read_file("data.txt", tc)

        assert result == "hello world"


def test_codeopt_list_sandbox_lists_files():
    with tempfile.TemporaryDirectory() as sandbox:
        Path(os.path.join(sandbox, "a.py")).write_text("x")
        Path(os.path.join(sandbox, "b.py")).write_text("y")
        tc = MockToolContext({"sandbox": sandbox})

        result = co_list_sandbox("", tc)

        assert "a.py" in result
        assert "b.py" in result


def test_codeopt_get_optimization_context_reads_structured_intent():
    tc = MockToolContext({
        "intent_extractor_output": {
            "connection": "pool", "queries": "crud", "transactions": "manual",
            "n_plus_one": "yes", "concurrency": "seq", "orm": "raw",
            "optimization_targets": [
                {"file": "driver.py", "description": "combine queries"},
                {"file": "loader.py", "description": "batch inserts"},
            ],
            "notes": "n/a",
        },
        "strategies": "COMBINING_QUERIES",
        "sandbox": "/tmp/sb",
    })

    result = co_get_optimization_context(tc)

    assert "CONNECTION: pool" in result
    assert "COMBINING_QUERIES" in result
    assert "driver.py" in result
    assert "loader.py" in result
    assert "combine queries" in result
    assert "/tmp/sb" in result


def test_codeopt_get_optimization_context_missing_intent_output():
    tc = MockToolContext({})

    result = co_get_optimization_context(tc)

    assert "ERROR" in result


def test_codeopt_get_optimization_context_no_targets():
    tc = MockToolContext({"intent_extractor_output": {"connection": "pool", "optimization_targets": []}})

    result = co_get_optimization_context(tc)

    assert "ERROR" in result


# ---------------------------------------------------------------------------
# verifier.tools
# ---------------------------------------------------------------------------

from src.rewriter.sub_agents.verifier.tools import check_syntax, run_application


def test_check_syntax_reports_ok_for_valid_python():
    with tempfile.TemporaryDirectory() as sandbox:
        Path(os.path.join(sandbox, "valid.py")).write_text("print('hello')\nx = 1\n")
        tc = MockToolContext({"sandbox": sandbox, "modified_files": ["valid.py"]})

        result = check_syntax(tc)

        assert "OK  valid.py" in result


def test_check_syntax_reports_fail_for_syntax_error():
    with tempfile.TemporaryDirectory() as sandbox:
        Path(os.path.join(sandbox, "broken.py")).write_text("def foo(\n")
        tc = MockToolContext({"sandbox": sandbox, "modified_files": ["broken.py"]})

        result = check_syntax(tc)

        assert "FAIL broken.py" in result


def test_check_syntax_skips_non_python():
    with tempfile.TemporaryDirectory() as sandbox:
        Path(os.path.join(sandbox, "data.json")).write_text("{}")
        tc = MockToolContext({"sandbox": sandbox, "modified_files": ["data.json"]})

        result = check_syntax(tc)

        assert "data.json" not in result


def test_check_syntax_no_modified_files():
    tc = MockToolContext({"sandbox": "/tmp", "modified_files": []})

    result = check_syntax(tc)

    assert "No modified files" in result


def test_run_application_started_ok_for_long_running_app():
    with tempfile.TemporaryDirectory() as sandbox:
        entry = "app.py"
        Path(os.path.join(sandbox, entry)).write_text(
            "import time\nprint('starting')\ntime.sleep(30)\n"
        )
        tc = MockToolContext({"sandbox": sandbox, "file_selector_output": {"entry_point": entry}})

        result = run_application("", tc)

        assert result.startswith("STARTED_OK")


def test_run_application_startup_failed_for_crashing_app():
    with tempfile.TemporaryDirectory() as sandbox:
        entry = "app.py"
        Path(os.path.join(sandbox, entry)).write_text("raise ImportError('boom')\n")
        tc = MockToolContext({"sandbox": sandbox, "file_selector_output": {"entry_point": entry}})

        result = run_application("", tc)

        assert result.startswith("STARTUP_FAILED_CODE")
        assert "boom" in result


def test_run_application_no_entry_point():
    tc = MockToolContext({"sandbox": "/tmp", "file_selector_output": {}})

    result = run_application("", tc)

    assert "ERROR" in result


def test_run_application_classified_as_db_error():
    with tempfile.TemporaryDirectory() as sandbox:
        entry = "app.py"
        Path(os.path.join(sandbox, entry)).write_text(
            "import MySQLdb\nraise MySQLdb.OperationalError(2002, \"Can't connect to local MySQL server through socket '/tmp/mysql.sock' (2)\")\n"
        )
        tc = MockToolContext({"sandbox": sandbox, "file_selector_output": {"entry_point": entry}})

        result = run_application("", tc)

        assert result.startswith("STARTUP_FAILED_ENV:DB")


def test_run_application_classified_as_missing_args():
    with tempfile.TemporaryDirectory() as sandbox:
        entry = "app.py"
        Path(os.path.join(sandbox, entry)).write_text(
            "import sys\nprint('usage: app.py <required>', file=sys.stderr)\nsys.exit(2)\n"
        )
        tc = MockToolContext({"sandbox": sandbox, "file_selector_output": {"entry_point": entry}})

        result = run_application("", tc)

        assert result.startswith("STARTUP_FAILED_ENV:MISSING_ARGS")


def test_run_application_classified_as_network_error():
    with tempfile.TemporaryDirectory() as sandbox:
        entry = "app.py"
        Path(os.path.join(sandbox, entry)).write_text(
            "raise ConnectionError('getaddrinfo failed')\n"
        )
        tc = MockToolContext({"sandbox": sandbox, "file_selector_output": {"entry_point": entry}})

        result = run_application("", tc)

        assert result.startswith("STARTUP_FAILED_ENV:NETWORK")


def test_run_application_classified_as_code_error():
    with tempfile.TemporaryDirectory() as sandbox:
        entry = "app.py"
        Path(os.path.join(sandbox, entry)).write_text("undefined_var\n")
        tc = MockToolContext({"sandbox": sandbox, "file_selector_output": {"entry_point": entry}})

        result = run_application("", tc)

        assert result.startswith("STARTUP_FAILED_CODE:CODE")


# ---------------------------------------------------------------------------
# intent_extractor.tools
# ---------------------------------------------------------------------------

from src.rewriter.sub_agents.intent_extractor.tools import read_selected_files


def test_read_selected_files_returns_contents():
    with tempfile.TemporaryDirectory() as root:
        Path(os.path.join(root, "a.py")).write_text("x = 1")
        Path(os.path.join(root, "b.py")).write_text("y = 2")
        tc = MockToolContext({"target": root, "file_selector_output": {"files": ["a.py", "b.py"], "entry_point": "main.py"}})

        result = read_selected_files(tc)

        assert "=== a.py ===" in result
        assert "x = 1" in result
        assert "y = 2" in result


def test_read_selected_files_missing_state():
    tc = MockToolContext({})

    result = read_selected_files(tc)

    assert "ERROR" in result


def test_read_selected_files_no_files():
    tc = MockToolContext({"target": "/tmp", "file_selector_output": {"files": [], "entry_point": "main.py"}})

    result = read_selected_files(tc)

    assert "ERROR" in result


# ---------------------------------------------------------------------------
# tools layer ADK wrappers
# ---------------------------------------------------------------------------

from src.rewriter.tools.scanner import scan_codebase
from src.rewriter.tools.copier import copy_to_sandbox
from src.rewriter.tools.planner import get_optimization_strategies


def test_scan_codebase_writes_scan_result_to_state():
    with tempfile.TemporaryDirectory() as root:
        Path(os.path.join(root, "app.py")).write_text("print('hi')\n")
        Path(os.path.join(root, "README.md")).write_text("docs")
        tc = MockToolContext({"target": root})

        result = scan_codebase(tc)

        assert "app.py" in result
        assert tc.state["scan_result"] == result


def test_scan_codebase_missing_target():
    tc = MockToolContext({})

    result = scan_codebase(tc)

    assert "ERROR" in result


def test_copy_to_sandbox_writes_sandbox_to_state():
    with tempfile.TemporaryDirectory() as root:
        Path(os.path.join(root, "app.py")).write_text("print('hi')\n")
        tc = MockToolContext({"target": root})

        result = copy_to_sandbox(tc)

        assert "OK" in result
        assert os.path.isdir(tc.state["sandbox"])
        assert os.path.isfile(os.path.join(tc.state["sandbox"], "app.py"))


def test_get_optimization_strategies_reads_structured_intent():
    tc = MockToolContext({
        "intent_extractor_output": {
            "connection": "pool",
            "queries": "select * from users",
            "optimization_targets": [{"file": "loader.py", "description": "n+1 in loop"}],
        }
    })

    result = get_optimization_strategies(tc)

    assert "strategies" in tc.state
    assert len(tc.state["strategies"]) > 0


def test_get_optimization_strategies_missing_intent_output():
    tc = MockToolContext({})

    result = get_optimization_strategies(tc)

    assert "ERROR" in result