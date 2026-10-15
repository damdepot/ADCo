"""Tests for the optimizer `replace_query_constant` tool (SINGLE_QUERY targets)."""

from src.code_rewriter.sub_agents.optimizer import tools
from src.code_rewriter.sub_agents.optimizer.tools import (
    get_optimization_context,
    replace_query_constant,
)
from src.code_rewriter.tools.query_constant import (
    get_class_constant,
    owning_class_name,
    replace_class_constant,
    target_constant_name,
)

APP = '''\
import psycopg2
from psycopg2.extras import RealDictCursor


class DatabaseClient:
    QUERY_1 = """\\
SELECT COUNT(*) FROM badges;
"""

    def run_query1(self):
        with self._conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(self.QUERY_1)
            results = cur.fetchall()
            return results
'''


class MockToolContext:
    def __init__(self, state=None):
        self.state = state if state is not None else {}


def _single_query_contract():
    target = {
        "file": "app.py",
        "function": "run_query1",
        "qualified_function": "DatabaseClient.run_query1",
    }
    return {
        "rewrite_id": "r1",
        "target": target,
        "targets": [target],
        "pattern": "SINGLE_QUERY",
        "strategy": "rewrite the QUERY_1 constant",
    }


def _setup(tmp_path):
    target_dir = tmp_path / "target"
    sandbox_dir = tmp_path / "sandbox"
    target_dir.mkdir()
    sandbox_dir.mkdir()
    (target_dir / "app.py").write_text(APP)
    (sandbox_dir / "app.py").write_text(APP)
    state = {
        "target": str(target_dir),
        "sandbox": str(sandbox_dir),
        "current_contract": _single_query_contract(),
        "attempt_count": 1,
    }
    return sandbox_dir, state


# ---------------------------------------------------------------------------
# pure helper logic (constant resolution + AST edit)
# ---------------------------------------------------------------------------

def test_target_constant_resolution_and_edit():
    assert owning_class_name(APP, "DatabaseClient.run_query1") == "DatabaseClient"
    assert target_constant_name(APP, "DatabaseClient.run_query1") == "QUERY_1"
    assert get_class_constant(APP, "DatabaseClient", "QUERY_1") == (
        True,
        "SELECT COUNT(*) FROM badges;\n",
    )

    ok, new_source, err = replace_class_constant(
        APP, "DatabaseClient", "QUERY_1", "SELECT COUNT(*) FROM users;"
    )
    assert ok, err
    assert get_class_constant(new_source, "DatabaseClient", "QUERY_1") == (
        True,
        "SELECT COUNT(*) FROM users;",
    )
    # The rest of the module is preserved.
    assert "def run_query1(self):" in new_source


# ---------------------------------------------------------------------------
# tool behavior
# ---------------------------------------------------------------------------

def test_replace_query_constant_applies(tmp_path):
    sandbox_dir, state = _setup(tmp_path)
    tc = MockToolContext(state)

    result = replace_query_constant(
        "app.py", "DatabaseClient.run_query1", "SELECT COUNT(*) FROM users;", tc
    )

    assert result.startswith("Successfully replaced")
    assert "QUERY_1" in result
    written = (sandbox_dir / "app.py").read_text()
    assert "SELECT COUNT(*) FROM users;" in written
    assert written.startswith("# ADCO_OPTIMIZED")
    assert tc.state["modified_files"] == ["app.py"]
    assert tc.state["optimizer_attempts"][0]["outcome"] == "APPLIED"


def test_replace_query_constant_rejects_unchanged_constant(tmp_path):
    sandbox_dir, state = _setup(tmp_path)
    tc = MockToolContext(state)

    result = replace_query_constant(
        "app.py", "DatabaseClient.run_query1", "SELECT COUNT(*) FROM badges;\n", tc
    )

    assert result.startswith("ERROR")
    assert "identical" in result
    assert (sandbox_dir / "app.py").read_text() == APP
    assert tc.state.get("modified_files") is None


def test_replace_query_constant_rejects_missing_constant(tmp_path):
    sandbox_dir, state = _setup(tmp_path)
    tc = MockToolContext(state)

    result = replace_query_constant(
        "app.py", "DatabaseClient.no_such_method", "SELECT 1", tc
    )

    assert result.startswith("ERROR")
    assert "QUERY_<n>" in result
    assert (sandbox_dir / "app.py").read_text() == APP


def test_replace_query_constant_rejects_empty_sql(tmp_path):
    sandbox_dir, state = _setup(tmp_path)
    tc = MockToolContext(state)

    result = replace_query_constant(
        "app.py", "DatabaseClient.run_query1", "   ", tc
    )

    assert result.startswith("ERROR")
    assert "non-empty" in result
    assert (sandbox_dir / "app.py").read_text() == APP


# ---------------------------------------------------------------------------
# optimization context
# ---------------------------------------------------------------------------

def test_get_optimization_context_single_query_renders_target(tmp_path):
    sandbox_dir, state = _setup(tmp_path)
    state["intent_extractor_output"] = {
        "connection": "pool", "queries": "crud", "transactions": "manual",
        "n_plus_one": "no", "concurrency": "seq", "orm": "raw",
        "optimization_targets": [{"file": "app.py", "description": "rewrite query"}],
        "notes": "n/a",
    }
    tc = MockToolContext(state)

    result = get_optimization_context(tc)

    assert "## SINGLE_QUERY target" in result
    assert "DatabaseClient" in result
    assert "QUERY_1" in result
    assert "SELECT COUNT(*) FROM badges;" in result
    assert "replace_query_constant" in result
    # The N+1 batching checklist must not be emitted for SINGLE_QUERY.
    assert "0 database operations inside any loop" not in result


# ---------------------------------------------------------------------------
# registration + prompt
# ---------------------------------------------------------------------------

def test_replace_query_constant_registered_in_agent():
    from src.code_rewriter.sub_agents.optimizer.agent import create_optimizer_agent

    agent = create_optimizer_agent()
    assert tools.replace_query_constant in agent.tools


def test_optimizer_prompt_mentions_replace_query_constant():
    from src.code_rewriter.sub_agents.optimizer.prompt import OPTIMIZER_AGENT_PROMPT

    assert "replace_query_constant" in OPTIMIZER_AGENT_PROMPT
    assert "SINGLE_QUERY" in OPTIMIZER_AGENT_PROMPT
