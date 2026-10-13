"""Tests for SINGLE_QUERY (constant-only) deterministic verification."""

from src.code_rewriter.models import RewriteContract, RewriteTarget
from src.code_rewriter.tools.ast_analyzer import analyze_source
from src.code_rewriter.tools.contract_verifier import (
    check_rewrite_coverage,
    is_single_query,
    verify_contract,
)

ORIGINAL = '''\
class DatabaseClient:
    QUERY_1 = """SELECT COUNT(*) FROM badges;"""

    def run_query1(self):
        with self._conn.cursor() as cur:
            cur.execute(self.QUERY_1)
            return cur.fetchall()
'''

CHANGED = ORIGINAL.replace(
    "SELECT COUNT(*) FROM badges;",
    "SELECT COUNT(*) FROM badges WHERE 1 = 1;",
)

NON_PLAIN = ORIGINAL.replace(
    'QUERY_1 = """SELECT COUNT(*) FROM badges;"""',
    'QUERY_1 = "SELECT COUNT(*)" + " FROM badges;"',
)

REMOVED = ORIGINAL.replace(
    '    QUERY_1 = """SELECT COUNT(*) FROM badges;"""\n\n',
    "",
)

NORMAL = '''\
def get_items(cursor, item_ids):
    result = {}
    for item_id in item_ids:
        cursor.execute("SELECT I_NAME FROM ITEM WHERE I_ID = %s", (item_id,))
        result[item_id] = cursor.fetchone()[0]
    return result
'''


def _single_query_contract():
    return RewriteContract(
        rewrite_id="test_single_query",
        target=RewriteTarget(
            file="repo.py",
            function="run_query1",
            qualified_function="DatabaseClient.run_query1",
        ),
        targets=[
            RewriteTarget(
                file="repo.py",
                function="run_query1",
                qualified_function="DatabaseClient.run_query1",
            )
        ],
        pattern="SINGLE_QUERY",
        strategy="PREDICATE_PUSHDOWN",
    )


def _coverage(opt_source, contract=None):
    return check_rewrite_coverage(
        analyze_source(ORIGINAL),
        analyze_source(opt_source),
        contract or _single_query_contract(),
        orig_source=ORIGINAL,
        opt_source=opt_source,
    )


def test_is_single_query():
    assert is_single_query(_single_query_contract())
    assert not is_single_query(
        RewriteContract(
            rewrite_id="x",
            target=RewriteTarget(file="t.py", function="f"),
            pattern="N+1 Query",
            strategy="Query Batching",
        )
    )


def test_unchanged_constant_is_missing_rewrite():
    result = _coverage(ORIGINAL)
    assert result.status == "FAIL"
    assert result.target_coverage[0].status == "MISSING_REWRITE"
    assert any(v.code == "MISSING_REWRITE" for v in result.violations)


def test_changed_plain_string_constant_is_transformed():
    result = _coverage(CHANGED)
    assert result.status == "PASS"
    assert result.target_coverage[0].status == "TRANSFORMED"
    assert result.violations == []


def test_changed_constant_passes_integration():
    result = verify_contract(ORIGINAL, CHANGED, _single_query_contract())
    assert result.status == "PASS", [v.model_dump() for v in result.violations]
    assert result.transformed_targets == 1


def test_non_string_constant_fails():
    result = _coverage(NON_PLAIN)
    assert result.status == "FAIL"
    assert result.target_coverage[0].status == "MISSING_REWRITE"
    assert any(
        v.code == "QUERY_CONSTANT_NOT_PLAIN_STRING" and v.severity == "ERROR"
        for v in result.violations
    )


def test_removed_constant_is_missing_rewrite():
    result = _coverage(REMOVED)
    assert result.status == "FAIL"
    assert result.target_coverage[0].status == "MISSING_REWRITE"
    assert any(v.code == "MISSING_REWRITE" for v in result.violations)


def test_non_single_query_unchanged_still_missing_rewrite():
    contract = RewriteContract(
        rewrite_id="test_n1",
        target=RewriteTarget(file="t.py", function="get_items", qualified_function="get_items"),
        targets=[RewriteTarget(file="t.py", function="get_items", qualified_function="get_items")],
        pattern="N+1 Query",
        strategy="Query Batching",
    )
    result = check_rewrite_coverage(
        analyze_source(NORMAL),
        analyze_source(NORMAL),
        contract,
        orig_source=NORMAL,
        opt_source=NORMAL,
    )
    assert result.status == "FAIL"
    assert result.target_coverage[0].status == "MISSING_REWRITE"
