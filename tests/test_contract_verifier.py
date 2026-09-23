import pytest
from pathlib import Path

from src.code_rewriter.models.rewrite_models import RewriteContract, RewriteTarget
from src.code_rewriter.tools.contract_verifier import verify_contract

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "code_rewriter"

def read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()

@pytest.fixture
def contract():
    return RewriteContract(
        rewrite_id="test_123",
        target=RewriteTarget(file="test.py", function="get_user_data"),
        pattern="N+1 Query",
        strategy="Replace loop with IN clause",
        allowed_regions=["get_user_data"],
        must_preserve=["return_type", "function_signature"],
        must_not_change=[]
    )

def test_valid_rewrite(contract):
    # Test 1: Valid rewrite (PASS)
    # Test 7: Valid SQL transformation allowed
    # Test 8: Unrelated function unchanged
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("optimized_n_plus_one.py")
    
    result = verify_contract(orig, opt, contract)
    assert result.status == "PASS"
    assert len(result.violations) == 0

def test_syntax_error(contract):
    # Test 2: Syntax error
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("invalid_optimized.py")
    
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "SYNTAX_ERROR" for v in result.violations)

def test_signature_changed(contract):
    # Test 3: Signature changed
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("signature_changed.py")
    
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "FUNCTION_SIGNATURE_CHANGED" for v in result.violations)

def test_target_missing(contract):
    # Test 4: Target removed / missing
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("target_missing.py")
    
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "TARGET_MISSING" for v in result.violations)

def test_unauthorized_change(contract):
    # Test 5: Unauthorized function changed
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("unauthorized_change.py")
    
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "UNAUTHORIZED_CHANGE" for v in result.violations)

def test_strategy_not_applied(contract):
    # Test 6: Strategy not applied
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("strategy_not_applied.py")
    
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "STRATEGY_NOT_APPLIED" for v in result.violations)

def test_return_behavior_changed(contract):
    # Test 9: Return behavior changed
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("return_behavior_changed.py")
    
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "RETURN_BEHAVIOR_CHANGED" for v in result.violations)

def test_determinism(contract):
    # Test 10: Determinism
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("optimized_n_plus_one.py")
    
    res1 = verify_contract(orig, opt, contract)
    res2 = verify_contract(orig, opt, contract)
    
    assert res1.model_dump() == res2.model_dump()

def test_checks_visibility_and_serialization(contract):
    # Test 11: Checks visibility in result
    # Test 12: model_dump() serialization
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("optimized_n_plus_one.py")
    
    result = verify_contract(orig, opt, contract)
    assert len(result.checks) > 0
    assert any(c.name == "check_syntax" for c in result.checks)
    
    dump = result.model_dump()
    assert dump["status"] == "PASS"
    assert "checks" in dump
    assert "violations" in dump


def test_check_dependency_integrity_pass():
    orig_code = """
class OrderService:
    def helper_method(self):
        return 42

    def process_order(self, order_ids):
        res = []
        for oid in order_ids:
            cursor.execute("SELECT * FROM orders WHERE id = %s", (oid,))
            res.append(cursor.fetchone())
        return res
"""
    opt_code = """
class OrderService:
    def helper_method(self):
        return 42

    def process_order(self, order_ids):
        cursor.execute("SELECT * FROM orders WHERE id = ANY(%s)", (order_ids,))
        return cursor.fetchall()
"""
    contract = RewriteContract(
        rewrite_id="test_integrity",
        target=RewriteTarget(file="service.py", qualified_function="OrderService.process_order"),
        pattern="N+1 Query Loop",
        strategy="Query Batching",
        allowed_regions=["OrderService.process_order"],
    )
    result = verify_contract(orig_code, opt_code, contract)
    assert result.status == "PASS"
    check_names = [c.name for c in result.checks]
    assert "check_dependency_integrity" in check_names


def test_untransformed_target_function(contract):
    # Target function unchanged from original AST
    orig = read_fixture("original_n_plus_one.py")
    result = verify_contract(orig, orig, contract)
    assert result.status == "FAIL"
    assert any(v.code in ("MISSING_REWRITE", "STRATEGY_NOT_APPLIED") for v in result.violations)


def _advisory_contract():
    return RewriteContract(
        rewrite_id="test_advisory",
        target=RewriteTarget(file="test.py", function="get_user_data"),
        pattern="N+1 Query",
        strategy="Replace loop with IN clause",
        allowed_regions=["get_user_data"],
        must_preserve=["return_type", "function_signature"],
    )


def test_assert_removal_is_warning_not_failure():
    orig = """
def get_user_data(user_ids):
    results = []
    for uid in user_ids:
        user = db.execute("SELECT * FROM users WHERE id = ?", uid)
        results.append(user)
    assert results is not None
    return results
"""
    opt = """
def get_user_data(user_ids):
    if not user_ids:
        return []
    placeholders = ",".join(["?"] * len(user_ids))
    results = db.execute(f"SELECT * FROM users WHERE id IN ({placeholders})", user_ids)
    return results
"""
    result = verify_contract(orig, opt, _advisory_contract())
    assert result.status == "PASS"
    assert any(v.code == "ASSERT_REMOVED" and v.severity == "WARNING" for v in result.violations)


def test_dead_local_is_warning_not_failure():
    orig = """
def get_user_data(user_ids):
    results = []
    for uid in user_ids:
        user = db.execute("SELECT * FROM users WHERE id = ?", uid)
        results.append(user)
    return results
"""
    opt = """
def get_user_data(user_ids):
    if not user_ids:
        return []
    unused = []
    placeholders = ",".join(["?"] * len(user_ids))
    results = db.execute(f"SELECT * FROM users WHERE id IN ({placeholders})", user_ids)
    return results
"""
    result = verify_contract(orig, opt, _advisory_contract())
    assert result.status == "PASS"
    assert any(v.code == "DEAD_LOCAL" and v.severity == "WARNING" for v in result.violations)


def test_error_violations_still_fail(contract):
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("signature_changed.py")
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.severity == "ERROR" for v in result.violations)


def test_rewrite_with_row_index_overflow_fails():
    orig = """
def get_stock(self, d_id, item_ids):
    result = {}
    for item_id in item_ids:
        self.cursor.execute("SELECT S_QUANTITY FROM STOCK WHERE S_I_ID = %s AND S_W_ID = %s", (item_id, d_id))
        result[item_id] = self.cursor.fetchone()[0]
    return result
"""
    opt = """
def get_stock(self, d_id, item_ids):
    placeholders = ",".join(["%s"] * len(item_ids))
    stock_sql = "SELECT S_QUANTITY, S_YTD, S_ORDER_CNT, S_REMOTE_CNT, S_DIST_01, S_DATA FROM STOCK WHERE (S_I_ID, S_W_ID) IN (" + placeholders + ")"
    self.cursor.execute(stock_sql, item_ids)
    stock_map = {(row[0], row[1]): (row[2], row[3], row[4], row[5], row[6], row[7]) for row in self.cursor.fetchall()}
    return stock_map
"""
    contract = RewriteContract(
        rewrite_id="test_row_overflow",
        target=RewriteTarget(file="t.py", function="get_stock"),
        pattern="N+1 Query",
        strategy="Query Batching",
        allowed_regions=["get_stock"],
    )
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "ROW_INDEX_OUT_OF_RANGE" for v in result.violations)


def test_verify_rejects_multi_statement_execute():
    orig = """
def get_stock(cursor, item_ids):
    result = {}
    for item_id in item_ids:
        cursor.execute("SELECT S_QUANTITY FROM STOCK WHERE S_I_ID = %s", (item_id,))
        result[item_id] = cursor.fetchone()[0]
    return result
"""
    opt = """
def get_stock(cursor, item_ids):
    cursor.execute("SELECT S_I_ID, S_QUANTITY FROM STOCK WHERE S_I_ID IN (%s); SELECT 1", (item_ids,))
    return {row[0]: row[1] for row in cursor.fetchall()}
"""
    contract = RewriteContract(
        rewrite_id="test_multi_statement",
        target=RewriteTarget(file="t.py", function="get_stock"),
        pattern="N+1 Query",
        strategy="Query Batching",
        allowed_regions=["get_stock"],
    )
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "MULTI_STATEMENT_EXECUTE" for v in result.violations)


def test_verify_rejects_duplicate_where():
    orig = """
def get_items(cursor, item_ids):
    result = {}
    for item_id in item_ids:
        cursor.execute("SELECT I_PRICE FROM ITEM WHERE I_ID = %s", (item_id,))
        result[item_id] = cursor.fetchone()[0]
    return result
"""
    opt = """
def get_items(cursor, item_ids):
    sql = "SELECT I_PRICE, I_NAME FROM ITEM WHERE I_ID = %s" + " WHERE I_ID IN (%s)"
    cursor.execute(sql, item_ids)
    return [row[0] for row in cursor.fetchall()]
"""
    contract = RewriteContract(
        rewrite_id="test_duplicate_where",
        target=RewriteTarget(file="t.py", function="get_items"),
        pattern="N+1 Query",
        strategy="Query Batching",
        allowed_regions=["get_items"],
    )
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "DUPLICATE_WHERE" for v in result.violations)


def test_verify_rejects_unknown_query_key():
    orig = """
TXN_QUERIES = {"DELIVERY": {"getNewOrder": "SELECT NO_O_ID FROM NEW_ORDER WHERE NO_D_ID = %s"}}

def do_delivery(cursor, item_ids):
    result = {}
    for item_id in item_ids:
        q = TXN_QUERIES["DELIVERY"]
        cursor.execute(q["getNewOrder"], (item_id,))
        result[item_id] = cursor.fetchone()[0]
    return result
"""
    opt = """
TXN_QUERIES = {"DELIVERY": {"getNewOrder": "SELECT NO_O_ID FROM NEW_ORDER WHERE NO_D_ID = %s"}}

def do_delivery(cursor, item_ids):
    q = TXN_QUERIES["DELIVERY"]
    cursor.execute(q["getNewOrderAll"], (item_ids,))
    return {row[0]: row[1] for row in cursor.fetchall()}
"""
    contract = RewriteContract(
        rewrite_id="test_unknown_key",
        target=RewriteTarget(file="t.py", function="do_delivery"),
        pattern="N+1 Query",
        strategy="Query Batching",
        allowed_regions=["do_delivery"],
    )
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "UNKNOWN_QUERY_KEY" for v in result.violations)




