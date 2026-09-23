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
    from src.intent_analyzer.sub_agents.file_selector.models import FileSelectorOutput
    m = FileSelectorOutput.model_validate({"files": ["a.py", "b.py"], "entry_point": "main.py"})
    assert m.files == ["a.py", "b.py"]
    assert m.entry_point == "main.py"


def test_optimizer_output_schema_validates():
    from src.code_rewriter.sub_agents.optimizer.models import OptimizerOutput
    m = OptimizerOutput.model_validate({"modified_files": ["a.py"], "summary": "ok"})
    assert m.modified_files == ["a.py"]
    assert m.summary == "ok"


def test_verifier_output_schema_validates_pass_and_fail():
    from src.code_rewriter.sub_agents.verifier.models import VerifierOutput
    assert VerifierOutput.model_validate({"status": "PASS", "category": "NONE", "reason": "ok", "detail": ""}).status == "PASS"
    assert VerifierOutput.model_validate({"status": "FAIL", "category": "name_error", "reason": "boom", "detail": "x"}).status == "FAIL"


def test_verifier_output_schema_rejects_invalid_status():
    from src.code_rewriter.sub_agents.verifier.models import VerifierOutput
    with pytest.raises(Exception):
        VerifierOutput.model_validate({"status": "MAYBE"})


def test_verifier_output_accepts_evidence_backed_issues():
    from src.code_rewriter.sub_agents.verifier.models import VerifierOutput
    from src.code_rewriter.models.feedback_models import RepairIssue
    out = VerifierOutput.model_validate({
        "status": "FAIL",
        "issues": [
            {
                "code": "SEMANTIC_ISSUE",
                "severity": "ERROR",
                "message": "wrong join",
                "evidence": "line 42: JOIN ...",
            }
        ],
    })
    assert len(out.issues) == 1
    assert isinstance(out.issues[0], RepairIssue)
    assert out.issues[0].evidence == "line 42: JOIN ..."


# ---------------------------------------------------------------------------
# optimizer.tools
# ---------------------------------------------------------------------------

from src.code_rewriter.sub_agents.optimizer.tools import (
    get_optimization_context as co_get_optimization_context,
    replace_function as co_replace_function,
)


def test_optimizer_get_optimization_context_missing_intent_output():
    tc = MockToolContext({})

    result = co_get_optimization_context(tc)

    assert "ERROR" in result


def test_optimizer_get_optimization_context_missing_current_contract():
    tc = MockToolContext({
        "intent_extractor_output": {
            "connection": "pool",
            "optimization_targets": [{"file": "driver.py"}],
        },
    })

    result = co_get_optimization_context(tc)

    assert "ERROR" in result
    assert "current_contract" in result


def test_optimizer_get_optimization_context_single_target():
    tc = MockToolContext({
        "intent_extractor_output": {
            "connection": "pool", "queries": "crud", "transactions": "manual",
            "n_plus_one": "yes", "concurrency": "seq", "orm": "raw",
            "optimization_targets": [
                {"file": "driver.py", "description": "batch new order queries"},
            ],
            "notes": "n/a",
        },
        "strategies": "QUERY_BATCHING",
        "sandbox": "/tmp/sb",
        "current_contract": {
            "rewrite_id": "r1",
            "target": {
                "file": "driver.py",
                "function": "doNewOrder",
                "qualified_function": "Db.doNewOrder",
            },
            "targets": [{"file": "driver.py", "function": "Db.doNewOrder"}],
            "pattern": "N_PLUS_ONE_QUERY",
            "strategy": "QUERY_BATCHING",
            "allowed_regions": ["Db.doNewOrder"],
            "must_preserve": ["function_signature"],
            "must_not_change": ["return_type"],
        },
        "target_context_map": {
            "Db.doNewOrder": {
                "analysis_summary": "## Function Analysis: Db.doNewOrder\n- Signature: def doNewOrder(self, warehouse_id)",
                "function_source": "def doNewOrder(self, warehouse_id):\n    cursor.execute('SELECT 1')\n",
                "dependency_slice": "# Dependency Slice: `doNewOrder`",
            },
        },
        "last_failure": {
            "status": "FAIL",
            "target_coverage": [
                {
                    "file": "driver.py",
                    "function": "Db.doNewOrder",
                    "status": "MISSING_REWRITE",
                    "details": "2 loop ops remain",
                },
            ],
            "violations": [
                {
                    "code": "STRATEGY_NOT_APPLIED",
                    "severity": "ERROR",
                    "message": "Strict-zero violation",
                },
            ],
        },
    })

    result = co_get_optimization_context(tc)

    assert "Contract" in result
    assert "Function Analysis" in result
    assert "Target Function Source" in result
    assert "def doNewOrder" in result
    assert "Acceptance Checklist" in result
    assert "0 database operations inside any loop" in result
    assert "Dependency Slice" in result
    assert "doNewOrder" in result
    assert "MISSING_REWRITE" in result
    assert "Strict-zero violation" in result
    assert "/tmp/sb" in result


def _write_gate_contract():
    return {
        "rewrite_id": "gate",
        "target": {"file": "app.py", "function": "get_user_data"},
        "targets": [{"file": "app.py", "function": "get_user_data"}],
        "pattern": "N_PLUS_ONE_QUERY",
        "strategy": "Replace loop with IN clause",
        "allowed_regions": ["get_user_data"],
        "must_preserve": ["return_type", "function_signature"],
    }


_ORIGINAL_LOOP_FN = (
    "def get_user_data(user_ids):\n"
    "    results = []\n"
    "    for uid in user_ids:\n"
    "        cursor.execute('SELECT * FROM users WHERE id = %s', (uid,))\n"
    "        results.append(cursor.fetchone())\n"
    "    return results\n"
)


def _write_gate_dirs(tmp_path, source):
    target_dir = tmp_path / "target"
    sandbox_dir = tmp_path / "sandbox"
    target_dir.mkdir()
    sandbox_dir.mkdir()
    (target_dir / "app.py").write_text(source)
    (sandbox_dir / "app.py").write_text(source)
    return target_dir, sandbox_dir


def test_replace_function_rejects_new_duplicate_where(tmp_path):
    target_dir, sandbox_dir = _write_gate_dirs(tmp_path, _ORIGINAL_LOOP_FN)
    candidate = (
        "def get_user_data(user_ids):\n"
        "    cursor.execute('SELECT * FROM users WHERE id = ANY(%s) WHERE name = %s', (user_ids, 'x'))\n"
        "    return cursor.fetchall()\n"
    )
    tc = MockToolContext({
        "target": str(target_dir),
        "sandbox": str(sandbox_dir),
        "current_contract": _write_gate_contract(),
    })

    result = co_replace_function("app.py", "get_user_data", candidate, tc)

    assert result.startswith("ERROR")
    assert "DUPLICATE_WHERE" in result
    assert (sandbox_dir / "app.py").read_text() == _ORIGINAL_LOOP_FN


def test_replace_function_rejects_unknown_query_key(tmp_path):
    original = (
        "QUERIES = {\n"
        "    'get_user': 'SELECT * FROM users WHERE id = %s',\n"
        "}\n\n"
        + _ORIGINAL_LOOP_FN.replace("cursor.execute('SELECT * FROM users WHERE id = %s', (uid,))", "cursor.execute(QUERIES['get_user'], (uid,))")
    )
    target_dir, sandbox_dir = _write_gate_dirs(tmp_path, original)
    candidate = (
        "def get_user_data(user_ids):\n"
        "    cursor.execute(QUERIES['bogus'], (user_ids,))\n"
        "    return cursor.fetchall()\n"
    )
    tc = MockToolContext({
        "target": str(target_dir),
        "sandbox": str(sandbox_dir),
        "current_contract": _write_gate_contract(),
    })

    result = co_replace_function("app.py", "get_user_data", candidate, tc)

    assert result.startswith("ERROR")
    assert "UNKNOWN_QUERY_KEY" in result
    assert (sandbox_dir / "app.py").read_text() == original


def test_replace_function_accepts_clean_batching_rewrite(tmp_path):
    target_dir, sandbox_dir = _write_gate_dirs(tmp_path, _ORIGINAL_LOOP_FN)
    candidate = (
        "def get_user_data(user_ids):\n"
        "    cursor.execute('SELECT * FROM users WHERE id = ANY(%s)', (user_ids,))\n"
        "    return cursor.fetchall()\n"
    )
    tc = MockToolContext({
        "target": str(target_dir),
        "sandbox": str(sandbox_dir),
        "current_contract": _write_gate_contract(),
    })

    result = co_replace_function("app.py", "get_user_data", candidate, tc)

    assert result.startswith("Successfully replaced")
    assert "ANY(%s)" in (sandbox_dir / "app.py").read_text()


# ---------------------------------------------------------------------------
# verifier.tools
# ---------------------------------------------------------------------------

from src.code_rewriter.sub_agents.verifier.tools import check_syntax, run_application


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


def test_run_application_deterministic_verification_pass():
    from src.code_rewriter.sub_agents.verifier.tools import run_contract_verification
    with tempfile.TemporaryDirectory() as target_dir, tempfile.TemporaryDirectory() as sandbox_dir:
        orig_code = (
            "def get_user_data(user_ids):\n"
            "    results = []\n"
            "    for uid in user_ids:\n"
            "        cursor.execute('SELECT * FROM users WHERE id = %s', (uid,))\n"
            "        results.append(cursor.fetchone())\n"
            "    return results\n"
        )
        opt_code = (
            "def get_user_data(user_ids):\n"
            "    cursor.execute('SELECT * FROM users WHERE id = ANY(%s)', (user_ids,))\n"
            "    return cursor.fetchall()\n"
        )
        Path(os.path.join(target_dir, "app.py")).write_text(orig_code)
        Path(os.path.join(sandbox_dir, "app.py")).write_text(opt_code)

        entry = "app.py"
        tc = MockToolContext({
            "target": target_dir,
            "sandbox": sandbox_dir,
            "modified_files": ["app.py"],
            "file_selector_output": {"entry_point": entry},
            "rewrite_contracts": [
                {
                    "rewrite_id": "test_pass",
                    "target": {"file": "app.py", "function": "get_user_data"},
                    "targets": [{"file": "app.py", "function": "get_user_data"}],
                    "pattern": "N+1 Query",
                    "strategy": "Replace loop with IN clause",
                    "allowed_regions": ["get_user_data"],
                    "must_preserve": ["return_type", "function_signature"],
                }
            ],
        })

        result = run_application("", tc)
        assert result.startswith("STARTED_OK")

        standalone = run_contract_verification(tc)
        assert "Deterministic Verification Status: PASS" in standalone


def test_run_application_deterministic_verification_fail_blocks_started_ok():
    from src.code_rewriter.sub_agents.verifier.tools import run_contract_verification
    with tempfile.TemporaryDirectory() as target_dir, tempfile.TemporaryDirectory() as sandbox_dir:
        # Code is untransformed (queries still in loop), but valid python that exits 0
        code = (
            "def get_user_data(user_ids):\n"
            "    results = []\n"
            "    for uid in user_ids:\n"
            "        cursor.execute('SELECT * FROM users WHERE id = %s', (uid,))\n"
            "        results.append(cursor.fetchone())\n"
            "    return results\n"
        )
        Path(os.path.join(target_dir, "app.py")).write_text(code)
        Path(os.path.join(sandbox_dir, "app.py")).write_text(code)

        entry = "app.py"
        tc = MockToolContext({
            "target": target_dir,
            "sandbox": sandbox_dir,
            "modified_files": ["app.py"],
            "file_selector_output": {"entry_point": entry},
            "rewrite_contracts": [
                {
                    "rewrite_id": "test_fail",
                    "target": {"file": "app.py", "function": "get_user_data"},
                    "targets": [{"file": "app.py", "function": "get_user_data"}],
                    "pattern": "N+1 Query",
                    "strategy": "Replace loop with IN clause",
                    "allowed_regions": ["get_user_data"],
                    "must_preserve": ["return_type", "function_signature"],
                }
            ],
        })

        result = run_application("", tc)
        assert result.startswith("STARTUP_FAILED_CODE:DETERMINISTIC_VERIFICATION_FAIL")
        assert "Violations:" in result
        assert "Target Coverage:" in result
        assert "MISSING_REWRITE" in result or "STRATEGY_NOT_APPLIED" in result
        assert tc.state["deterministic_verification"]["status"] == "FAIL"

        standalone = run_contract_verification(tc)
        assert "Deterministic Verification Status: FAIL" in standalone


def test_get_verification_context():
    from src.code_rewriter.sub_agents.verifier.tools import get_verification_context
    with tempfile.TemporaryDirectory() as target_dir, tempfile.TemporaryDirectory() as sandbox_dir:
        orig_code = (
            "def get_user_data(user_ids):\n"
            "    # ORIGINAL_MARKER\n"
            "    results = []\n"
            "    for uid in user_ids:\n"
            "        cursor.execute('SELECT * FROM users WHERE id = %s', (uid,))\n"
            "        results.append(cursor.fetchone())\n"
            "    return results\n"
        )
        opt_code = (
            "def get_user_data(user_ids):\n"
            "    # OPTIMIZED_MARKER\n"
            "    cursor.execute('SELECT * FROM users WHERE id = ANY(%s)', (user_ids,))\n"
            "    return cursor.fetchall()\n"
        )
        Path(os.path.join(target_dir, "app.py")).write_text(orig_code)
        Path(os.path.join(sandbox_dir, "app.py")).write_text(opt_code)

        tc = MockToolContext({
            "target": target_dir,
            "sandbox": sandbox_dir,
            "modified_files": ["app.py"],
            "rewrite_contracts": [
                {
                    "rewrite_id": "ctx_test",
                    "target": {"file": "app.py", "function": "get_user_data"},
                    "targets": [{"file": "app.py", "function": "get_user_data"}],
                    "pattern": "N_PLUS_ONE_QUERY",
                    "strategy": "Replace loop with IN clause",
                    "allowed_regions": ["get_user_data"],
                    "must_preserve": ["return_type", "function_signature"],
                }
            ],
            "target_context_map": {
                "get_user_data": {
                    "analysis_summary": "## Function Analysis: get_user_data\n- Signature: def get_user_data(user_ids)",
                    "function_source": orig_code,
                    "dependency_slice": "# Dependency Slice: `get_user_data`",
                },
            },
        })

        result = get_verification_context(tc)

        assert "N_PLUS_ONE_QUERY" in result
        assert "Function Analysis: get_user_data" in result
        assert "ORIGINAL_MARKER" in result
        assert "OPTIMIZED_MARKER" in result


def test_get_verification_context_scopes_to_current_contract():
    from src.code_rewriter.sub_agents.verifier.tools import get_verification_context
    with tempfile.TemporaryDirectory() as target_dir, tempfile.TemporaryDirectory() as sandbox_dir:
        code = "def f():\n    return 1\n"
        Path(os.path.join(target_dir, "app.py")).write_text(code)
        Path(os.path.join(sandbox_dir, "app.py")).write_text(code)

        current = {
            "rewrite_id": "c1",
            "target": {"file": "app.py", "function": "f"},
            "targets": [{"file": "app.py", "function": "f"}],
            "pattern": "N_PLUS_ONE_QUERY",
            "strategy": "batch",
            "allowed_regions": ["f"],
            "must_preserve": [],
        }
        other = {
            "rewrite_id": "c2",
            "target": {"file": "app.py", "function": "g"},
            "targets": [{"file": "app.py", "function": "g"}],
            "pattern": "N_PLUS_ONE_QUERY",
            "strategy": "batch",
            "allowed_regions": ["g"],
            "must_preserve": [],
        }
        tc = MockToolContext({
            "target": target_dir,
            "sandbox": sandbox_dir,
            "modified_files": ["app.py"],
            "rewrite_contracts": [current, other],
            "current_contract": current,
            "target_context_map": {
                "f": {"analysis_summary": "SUMMARY_F", "function_source": code},
                "g": {"analysis_summary": "SUMMARY_G", "function_source": code},
            },
        })

        result = get_verification_context(tc)

        assert "SUMMARY_F" in result
        assert "SUMMARY_G" not in result



# ---------------------------------------------------------------------------
# intent_extractor.tools
# ---------------------------------------------------------------------------

from src.intent_analyzer.sub_agents.intent_extractor.tools import read_selected_files


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

from src.code_rewriter.tools.copier import copy_to_sandbox
from src.code_rewriter.tools.planner import get_optimization_strategies


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
    assert result  # strategy summary returned


def test_get_optimization_strategies_missing_intent_output():
    tc = MockToolContext({})

    result = get_optimization_strategies(tc)

    assert "ERROR" in result
