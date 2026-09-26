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
    _render_previous_attempt,
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


def _optimizer_context_state(attempts):
    return {
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
        "optimizer_attempts": attempts,
    }


def test_get_optimization_context_includes_prior_attempts():
    tc = MockToolContext(_optimizer_context_state([
        {
            "file": "driver.py",
            "function": "Db.doNewOrder",
            "bare_function": "doNewOrder",
            "outcome": "REJECTED",
            "codes": ["DUPLICATE_WHERE"],
            "message": "boom",
            "attempt": 1,
            "candidate_key": "k",
            "diff": "- old\n+ new",
        }
    ]))

    result = co_get_optimization_context(tc)

    assert "## Prior Optimizer Attempts" in result
    assert "boom" in result

    no_match = MockToolContext(_optimizer_context_state([]))
    assert "## Prior Optimizer Attempts" not in co_get_optimization_context(no_match)


_NO_REWRITE_FN = "def doNewOrder(self, warehouse_id):\n    cursor.execute('SELECT 1')\n"


def _optimizer_context_state_with_sandbox(
    tmp_path, sandbox_function_source, attempt_count
):
    state = _optimizer_context_state([])
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "driver.py").write_text(sandbox_function_source)
    state["sandbox"] = str(sandbox)
    state["attempt_count"] = attempt_count
    return state


def test_get_optimization_context_no_rewrite_directive_when_unchanged(tmp_path):
    state = _optimizer_context_state_with_sandbox(tmp_path, _NO_REWRITE_FN, 2)

    result = co_get_optimization_context(MockToolContext(state))

    assert "## CRITICAL: No rewrite applied" in result


def test_get_optimization_context_no_directive_on_first_attempt(tmp_path):
    state = _optimizer_context_state_with_sandbox(tmp_path, _NO_REWRITE_FN, 1)

    result = co_get_optimization_context(MockToolContext(state))

    assert "## CRITICAL: No rewrite applied" not in result


def test_get_optimization_context_no_directive_when_modified(tmp_path):
    modified = (
        "def doNewOrder(self, warehouse_id):\n"
        "    cursor.execute('SELECT * FROM orders')\n"
    )
    state = _optimizer_context_state_with_sandbox(tmp_path, modified, 2)

    result = co_get_optimization_context(MockToolContext(state))

    assert "## CRITICAL: No rewrite applied" not in result


def test_render_previous_attempt_diff_ignores_indentation(tmp_path):
    from src.code_rewriter._common import extract_function_source_by_name

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    method_source = (
        "class Db:\n"
        "    def doNewOrder(self, warehouse_id):\n"
        "        cursor.execute('SELECT 1')\n"
    )
    (sandbox / "driver.py").write_text(method_source)
    original_source = extract_function_source_by_name(method_source, "Db.doNewOrder")

    rendered = "\n".join(
        _render_previous_attempt(
            {"sandbox": str(sandbox)}, "driver.py", "Db.doNewOrder", original_source
        )
    )

    assert "## Diff vs Original" in rendered
    assert "(no changes)" in rendered


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


def test_replace_function_records_rejected_attempt(tmp_path):
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
    entries = tc.state["optimizer_attempts"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["outcome"] == "REJECTED"
    assert "DUPLICATE_WHERE" in entry["codes"]
    assert entry["candidate_key"]


def test_replace_function_duplicate_rejection_warns_and_sets_no_progress(tmp_path):
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

    co_replace_function("app.py", "get_user_data", candidate, tc)
    second = co_replace_function("app.py", "get_user_data", candidate, tc)
    third = co_replace_function("app.py", "get_user_data", candidate, tc)

    assert "already been rejected" in second
    assert "NO PROGRESS" in third
    assert tc.state["optimizer_no_progress"] == {
        "file": "app.py",
        "function": "get_user_data",
    }


def test_replace_function_whitespace_variant_counts_as_duplicate(tmp_path):
    target_dir, sandbox_dir = _write_gate_dirs(tmp_path, _ORIGINAL_LOOP_FN)
    candidate = (
        "def get_user_data(user_ids):\n"
        "    cursor.execute('SELECT * FROM users WHERE id = ANY(%s) WHERE name = %s', (user_ids, 'x'))\n"
        "    return cursor.fetchall()\n"
    )
    variant = (
        "def get_user_data(user_ids):\n"
        "\n"
        "    cursor.execute('SELECT * FROM users WHERE id = ANY(%s) WHERE name = %s', (user_ids, 'x'))\n"
        "    return cursor.fetchall()\n"
    )
    tc = MockToolContext({
        "target": str(target_dir),
        "sandbox": str(sandbox_dir),
        "current_contract": _write_gate_contract(),
    })

    co_replace_function("app.py", "get_user_data", candidate, tc)
    second = co_replace_function("app.py", "get_user_data", variant, tc)

    assert "already been rejected" in second


def test_replace_function_records_applied_attempt(tmp_path):
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
    entries = tc.state["optimizer_attempts"]
    assert len(entries) == 1
    assert entries[0]["outcome"] == "APPLIED"


def test_replace_function_rejects_ast_identical_rewrite(tmp_path):
    """Text-only changes (comments/whitespace/quotes) keep the AST identical and
    must be rejected, matching the deterministic verifier's AST comparison."""
    target_dir, sandbox_dir = _write_gate_dirs(tmp_path, _ORIGINAL_LOOP_FN)
    candidate = (
        "def get_user_data(user_ids):\n"
        "    # reformatted only -- no real change\n"
        "    results = []\n"
        "\n"
        "    for uid in user_ids:\n"
        '        cursor.execute("SELECT * FROM users WHERE id = %s", (uid,))\n'
        "        results.append(cursor.fetchone())\n"
        "    return results\n"
    )
    tc = MockToolContext({
        "target": str(target_dir),
        "sandbox": str(sandbox_dir),
        "current_contract": _write_gate_contract(),
    })

    result = co_replace_function("app.py", "get_user_data", candidate, tc)

    assert result.startswith("ERROR")
    assert "structurally" in result
    assert (sandbox_dir / "app.py").read_text() == _ORIGINAL_LOOP_FN


def test_replace_function_rejects_percent_format_arity_collision(tmp_path):
    target_dir, sandbox_dir = _write_gate_dirs(tmp_path, _ORIGINAL_LOOP_FN)
    candidate = (
        "def get_user_data(user_ids):\n"
        "    placeholders = ','.join(['%s'] * len(user_ids))\n"
        "    sql = (f'SELECT * FROM users WHERE id = %02d AND id IN ({placeholders})') % (user_ids,)\n"
        "    cursor.execute(sql, user_ids)\n"
        "    return cursor.fetchall()\n"
    )
    tc = MockToolContext({
        "target": str(target_dir),
        "sandbox": str(sandbox_dir),
        "current_contract": _write_gate_contract(),
    })

    result = co_replace_function("app.py", "get_user_data", candidate, tc)

    assert result.startswith("ERROR")
    assert "PERCENT_FORMAT_ARITY" in result
    assert (sandbox_dir / "app.py").read_text() == _ORIGINAL_LOOP_FN


_TPCC_DRIVER = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "tools"
    / "tpcc"
    / "drivers"
    / "postgresdriver.py"
)


# The real TPC-C doStockLevel (two sequential, value-dependent reads) rewritten
# into a single 3-relation JOIN: the canonical HIGH-risk transformation.
_HIGH_RISK_STOCK_LEVEL = (
    "def doStockLevel(self, params):\n"
    "    w_id = params[\"w_id\"]\n"
    "    d_id = params[\"d_id\"]\n"
    "    threshold = params[\"threshold\"]\n"
    "    self.cursor.execute(\n"
    "        \"SELECT COUNT(DISTINCT OL_I_ID) FROM ORDER_LINE \"\n"
    "        \"JOIN STOCK ON ORDER_LINE.OL_I_ID = STOCK.S_I_ID \"\n"
    "        \"JOIN DISTRICT ON STOCK.S_W_ID = DISTRICT.D_W_ID \"\n"
    "        \"WHERE OL_W_ID = %s AND OL_D_ID = %s AND OL_O_ID < %s\",\n"
    "        [w_id, d_id, threshold],\n"
    "    )\n"
    "    result = self.cursor.fetchone()\n"
    "    self.conn.commit()\n"
    "    return int(result[0])\n"
)


# A dependency-preserving variant: the same two statements (and their value
# dependency) are kept separate, so the risk stays LOW.
_LOW_RISK_STOCK_LEVEL = (
    "def doStockLevel(self, params):\n"
    "    q = TXN_QUERIES[\"STOCK_LEVEL\"]\n"
    "    w_id = params[\"w_id\"]\n"
    "    d_id = params[\"d_id\"]\n"
    "    threshold = params[\"threshold\"]\n"
    "    self.cursor.execute(q[\"getOId\"], [w_id, d_id])\n"
    "    result = self.cursor.fetchone()\n"
    "    if not result:\n"
    "        raise RuntimeError(\"missing order\")\n"
    "    o_id = result[0]\n"
    "    self.cursor.execute(\n"
    "        q[\"getStockCount\"], [w_id, d_id, o_id, (o_id - 20), w_id, threshold]\n"
    "    )\n"
    "    result = self.cursor.fetchone()\n"
    "    self.conn.commit()\n"
    "    return int(result[0])\n"
)


def _stock_level_contract():
    return {
        "rewrite_id": "stock_level",
        "target": {
            "file": "postgresdriver.py",
            "function": "doStockLevel",
            "qualified_function": "PostgresDriver.doStockLevel",
        },
        "targets": [
            {
                "file": "postgresdriver.py",
                "function": "doStockLevel",
                "qualified_function": "PostgresDriver.doStockLevel",
            }
        ],
        "pattern": "N_PLUS_ONE_QUERY",
        "strategy": "combine dependent reads",
        "allowed_regions": ["PostgresDriver.doStockLevel"],
        "must_preserve": ["return_type", "function_signature"],
    }


def _stock_level_setup(tmp_path):
    from src.code_rewriter.tools.ast_analyzer import analyze_file
    from src.code_rewriter.tools.db_interaction import build_read_write_map

    original = _TPCC_DRIVER.read_text(encoding="utf-8")
    target_dir = tmp_path / "target"
    sandbox_dir = tmp_path / "sandbox"
    target_dir.mkdir()
    sandbox_dir.mkdir()
    (target_dir / "postgresdriver.py").write_text(original, encoding="utf-8")
    (sandbox_dir / "postgresdriver.py").write_text(original, encoding="utf-8")
    state = {
        "target": str(target_dir),
        "sandbox": str(sandbox_dir),
        "current_contract": _stock_level_contract(),
        "read_write_map": build_read_write_map(analyze_file(_TPCC_DRIVER)),
    }
    return sandbox_dir, original, state


def test_replace_function_blocks_high_risk_stock_level(tmp_path):
    sandbox_dir, original, state = _stock_level_setup(tmp_path)
    tc = MockToolContext(state)

    result = co_replace_function(
        "postgresdriver.py", "PostgresDriver.doStockLevel", _HIGH_RISK_STOCK_LEVEL, tc
    )

    assert result.startswith("ERROR")
    assert "HIGH transformation risk" in result
    assert "DEPENDENT_QUERY_FUSION" in result
    assert (sandbox_dir / "postgresdriver.py").read_text(encoding="utf-8") == original
    assert "PostgresDriver.doStockLevel" in tc.state["risk_rejections"]


def test_replace_function_accepts_low_risk_stock_level(tmp_path):
    sandbox_dir, original, state = _stock_level_setup(tmp_path)
    tc = MockToolContext(state)

    result = co_replace_function(
        "postgresdriver.py", "PostgresDriver.doStockLevel", _LOW_RISK_STOCK_LEVEL, tc
    )

    assert result.startswith("Successfully replaced")
    assert (sandbox_dir / "postgresdriver.py").read_text(encoding="utf-8") != original
    assert not tc.state.get("risk_rejections")


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
