"""Smoke test for the SINGLE_QUERY path (deterministic, no LLM/DB).

Exercises the whole chain on a tiny app: fallback discovery -> per-target
constant rewrite via the optimizer tool -> deterministic verification ->
decompiler extraction of the rewritten SQL.
"""

from src.adco.pydecompiler import decompile_app
from src.code_rewriter.sub_agents.optimizer.tools import replace_query_constant
from src.code_rewriter.tools.contract_verifier import verify_contract
from src.code_rewriter.tools.pipeline_analysis import (
    build_contracts_from_intent,
    verify_all_contracts,
)

DB_APP = '''\
class DatabaseClient:
    QUERY_1 = """SELECT COUNT(*) FROM badges;"""
    QUERY_2 = """SELECT COUNT(*) FROM users;"""
    QUERY_3 = """SELECT COUNT(*) FROM posts;"""

    def run_query1(self):
        with self._conn.cursor() as cur:
            cur.execute(self.QUERY_1)
            return cur.fetchall()

    def run_query2(self):
        with self._conn.cursor() as cur:
            cur.execute(self.QUERY_2)
            return cur.fetchall()

    def run_query3(self):
        with self._conn.cursor() as cur:
            cur.execute(self.QUERY_3)
            return cur.fetchall()


def main():
    client = DatabaseClient()
    client.run_query1()
    client.run_query2()
    client.run_query3()
'''

REWRITES = {
    "DatabaseClient.run_query1": "SELECT COUNT(*) FROM badges WHERE UserId > 0;",
    "DatabaseClient.run_query2": "SELECT COUNT(*) FROM users WHERE Reputation > 0;",
    "DatabaseClient.run_query3": "SELECT COUNT(*) FROM posts WHERE Score > 0;",
}


class MockToolContext:
    def __init__(self, state=None):
        self.state = state if state is not None else {}


def test_single_query_smoke_end_to_end(tmp_path):
    target_dir = tmp_path / "target"
    sandbox_dir = tmp_path / "sandbox"
    target_dir.mkdir()
    sandbox_dir.mkdir()
    (target_dir / "db_app.py").write_text(DB_APP)
    (sandbox_dir / "db_app.py").write_text(DB_APP)

    intent = {"optimization_targets": [{"file": "db_app.py"}]}
    _analyses, contracts = build_contracts_from_intent(str(target_dir), intent)
    assert len(contracts) == 3
    assert all(c.pattern == "SINGLE_QUERY" for c in contracts)

    state = {
        "target": str(target_dir),
        "sandbox": str(sandbox_dir),
        "rewrite_contracts": [c.model_dump() for c in contracts],
        "modified_files": [],
        "attempt_count": 1,
    }
    tc = MockToolContext(state)

    # Rewrite every target's constant through the optimizer tool.
    for contract in contracts:
        qfn = contract.target.qualified_function
        tc.state["current_contract"] = contract.model_dump()
        result = replace_query_constant("db_app.py", qfn, REWRITES[qfn], tc)
        assert result.startswith("Successfully replaced"), result

    assert state["modified_files"] == ["db_app.py"]

    # Every target must verify as TRANSFORMED (constant-only change).
    original = (target_dir / "db_app.py").read_text()
    rewritten = (sandbox_dir / "db_app.py").read_text()
    for contract in contracts:
        result = verify_contract(original, rewritten, contract)
        assert result.status == "PASS", (contract.target.function, result.violations)
        assert [tc.status for tc in result.target_coverage] == ["TRANSFORMED"]

    aggregate = verify_all_contracts(
        str(target_dir), str(sandbox_dir), contracts, state["modified_files"]
    )
    assert aggregate.status == "PASS"
    assert aggregate.transformed_targets == 3
    assert aggregate.rewrite_coverage == 1.0

    # The rewritten SQL must flow through the decompiler (what the benchmark reads).
    decompiled = tmp_path / "decompiled"
    decompile_app(str(sandbox_dir), str(decompiled))
    for num, contract in enumerate(contracts, start=1):
        sql = (decompiled / f"query{num}.sql").read_text()
        assert REWRITES[contract.target.qualified_function] in sql
