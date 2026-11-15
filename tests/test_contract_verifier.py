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


def test_verify_rejects_implicit_cross_join():
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
    sql = "SELECT COUNT(DISTINCT OL_I_ID) FROM ORDER_LINE, STOCK, DISTRICT WHERE OL_W_ID = D_W_ID AND S_I_ID = OL_I_ID"
    cursor.execute(sql, (item_ids,))
    return {row[0]: row[1] for row in cursor.fetchall()}
"""
    contract = RewriteContract(
        rewrite_id="test_implicit_join",
        target=RewriteTarget(file="t.py", function="get_stock"),
        pattern="N+1 Query",
        strategy="Query Batching",
        allowed_regions=["get_stock"],
    )
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "IMPLICIT_CROSS_JOIN" for v in result.violations)


def test_set_based_batch_execute_in_grouping_loop_passes():
    orig = """
def process_items(cursor, keys):
    result = {}
    for a, b in keys:
        cursor.execute("SELECT V FROM T WHERE (A, B) = (%s, %s)", (a, b))
        result[(a, b)] = cursor.fetchone()[0]
    return result
"""
    opt = """
def process_items(cursor, keys):
    result = {}
    groups = {}
    for a, b in keys:
        if a not in groups:
            groups[a] = []
        groups[a].append(b)
    for a, bs in groups.items():
        cursor.execute("SELECT A, B, V FROM T WHERE A = %s AND B IN (%s)", (a, bs))
        rows = cursor.fetchall()
        for row in rows:
            result[(row[0], row[1])] = row[2]
    cursor.executemany("INSERT INTO T2 (A, B, V) VALUES (%s, %s, %s)", list(result.items()))
    return result
"""
    contract = RewriteContract(
        rewrite_id="test_set_based_batch",
        target=RewriteTarget(file="t.py", function="process_items"),
        targets=[RewriteTarget(file="t.py", function="process_items")],
        pattern="N+1 Query",
        strategy="Query Batching",
        allowed_regions=["process_items"],
    )
    result = verify_contract(orig, opt, contract)
    assert result.status == "PASS", [v.model_dump() for v in result.violations]


def test_per_row_execute_in_loop_still_fails():
    orig = """
def process_items(cursor, item_ids):
    result = {}
    for item_id in item_ids:
        cursor.execute("SELECT I_NAME FROM ITEM WHERE I_ID = %s", (item_id,))
        result[item_id] = cursor.fetchone()[0]
    return result
"""
    opt = """
def process_items(cursor, item_ids):
    result = {}
    for item_id in item_ids:
        cursor.execute("SELECT I_NAME FROM ITEM WHERE I_ID = %s", (item_id,))
        result[item_id] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM ITEM", ())
    return result
"""
    contract = RewriteContract(
        rewrite_id="test_per_row_loop",
        target=RewriteTarget(file="t.py", function="process_items"),
        pattern="N+1 Query",
        strategy="Query Batching",
        allowed_regions=["process_items"],
    )
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code in ("STRATEGY_NOT_APPLIED", "MISSING_REWRITE") for v in result.violations)


def test_fetch_inside_loop_is_ignored():
    orig = """
def get_items(cursor, item_ids):
    result = {}
    for item_id in item_ids:
        cursor.execute("SELECT I_NAME FROM ITEM WHERE I_ID = %s", (item_id,))
        result[item_id] = cursor.fetchone()[0]
    return result
"""
    opt = """
def get_items(cursor, item_ids):
    cursor.execute("SELECT I_ID, I_NAME FROM ITEM WHERE I_ID = ANY(%s)", (item_ids,))
    result = {}
    for item_id in item_ids:
        rows = cursor.fetchall()
        result[item_id] = rows
    return result
"""
    contract = RewriteContract(
        rewrite_id="test_fetch_loop",
        target=RewriteTarget(file="t.py", function="get_items"),
        targets=[RewriteTarget(file="t.py", function="get_items")],
        pattern="N+1 Query",
        strategy="Query Batching",
        allowed_regions=["get_items"],
    )
    result = verify_contract(orig, opt, contract)
    assert result.status == "PASS", [v.model_dump() for v in result.violations]


def test_verify_rejects_duplicate_column_predicate():
    orig = """
def get_stock(self, d_id, item_ids):
    result = {}
    for item_id in item_ids:
        self.cursor.execute("SELECT S_QUANTITY FROM STOCK WHERE S_I_ID = %s AND S_W_ID = %s", (item_id, d_id))
        result[item_id] = self.cursor.fetchone()[0]
    return result
"""
    opt = """
TXN_QUERIES = {"NEW_ORDER": {"getStockInfo": "SELECT S_QUANTITY, S_DATA FROM STOCK WHERE S_I_ID = %%s AND S_W_ID = %%s"}}

def get_stock(self, d_id, item_ids):
    d_id = 1
    q = TXN_QUERIES["NEW_ORDER"]
    stock_sql = (q["getStockInfo"] % (d_id)) + " AND S_I_ID IN (%s)"
    self.cursor.execute(stock_sql, [d_id] + item_ids)
    return {row[0]: row[1] for row in self.cursor.fetchall()}
"""
    contract = RewriteContract(
        rewrite_id="test_duplicate_column",
        target=RewriteTarget(file="t.py", function="get_stock"),
        pattern="N+1 Query",
        strategy="Query Batching",
        allowed_regions=["get_stock"],
    )
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "DUPLICATE_COLUMN_PREDICATE" for v in result.violations)


def test_verify_clean_composite_in_batch_passes():
    orig = """
def get_stock(self, d_id, item_ids):
    result = {}
    for item_id in item_ids:
        self.cursor.execute("SELECT S_QUANTITY FROM STOCK WHERE S_I_ID = %s AND S_W_ID = %s", (item_id, d_id))
        result[item_id] = self.cursor.fetchone()[0]
    return result
"""
    opt = """
TXN_QUERIES = {"NEW_ORDER": {"getStockInfo": "SELECT S_I_ID, S_QUANTITY FROM STOCK WHERE S_I_ID = %%s AND S_W_ID = %%s"}}

def get_stock(self, d_id, item_ids):
    q = TXN_QUERIES["NEW_ORDER"]
    placeholders = ",".join(["%s"] * len(item_ids))
    stock_sql = (q["getStockInfo"] % (d_id)).replace("WHERE S_I_ID = %s AND S_W_ID = %s", "WHERE (S_I_ID, S_W_ID) IN (" + placeholders + ")")
    self.cursor.execute(stock_sql, item_ids)
    return {row[0]: row[1] for row in self.cursor.fetchall()}
"""
    contract = RewriteContract(
        rewrite_id="test_clean_composite",
        target=RewriteTarget(file="t.py", function="get_stock"),
        pattern="N+1 Query",
        strategy="Query Batching",
        allowed_regions=["get_stock"],
    )
    result = verify_contract(orig, opt, contract)
    assert result.status == "PASS", [v.model_dump() for v in result.violations]


def _static_check_contract(function: str = "get_stock"):
    return RewriteContract(
        rewrite_id="test_static_check",
        target=RewriteTarget(file="t.py", function=function),
        pattern="N+1 Query",
        strategy="Query Batching",
        allowed_regions=[function],
    )


def test_verify_rejects_percent_format_arity_collision():
    orig = """
def get_stock(self, d_id):
    return None
"""
    opt = """
def get_stock(self, d_id):
    stock_placeholders = "(%s, %s), (%s, %s)"
    sql = (f"SELECT S_DIST_%02d FROM STOCK WHERE (S_I_ID, S_W_ID) IN ({stock_placeholders})") % (d_id,)
    self.cursor.execute(sql)
    return None
"""
    result = verify_contract(orig, opt, _static_check_contract())
    assert result.status == "FAIL"
    assert any(
        v.code == "PERCENT_FORMAT_ARITY" and v.severity == "ERROR"
        for v in result.violations
    )


def test_verify_accepts_escaped_percent_placeholders():
    orig = """
def get_stock(self, d_id):
    return None
"""
    opt = """
def get_stock(self, d_id):
    template = "SELECT S_DIST_%02d FROM STOCK WHERE S_I_ID = %%s AND S_W_ID = %%s"
    return template % (d_id,)
"""
    result = verify_contract(orig, opt, _static_check_contract())
    assert not any(v.code == "PERCENT_FORMAT_ARITY" for v in result.violations)


def test_verify_rejects_undefined_name_in_comprehension():
    orig = """
def get_stock(self, stock_rows):
    return None
"""
    opt = """
def get_stock(self, stock_rows):
    stock_map = {(row[0], row[1]): (row[2], row[3])}
    return stock_map
"""
    result = verify_contract(orig, opt, _static_check_contract())
    assert result.status == "FAIL"
    assert any(
        v.code == "UNDEFINED_NAME" and v.severity == "ERROR"
        for v in result.violations
    )


def test_verify_rejects_undefined_name_with_star_import():
    """A star import must not hide a new undefined name (tpcc production bug)."""
    orig = """
from .abstractdriver import *


class PostgresDriver(Driver):
    def doNewOrder(self, item_rows):
        item_map = {row[0]: (row[1], row[2], row[3]) for row in item_rows}
        return item_map
"""
    opt = """
from .abstractdriver import *


class PostgresDriver(Driver):
    def doNewOrder(self, item_rows):
        item_map = {row[0]: (row[1], row[2], row[3]) for row in item_rows}
        self.cursor.execute('SELECT 1', item_tuple)
        return item_map
"""
    contract = RewriteContract(
        rewrite_id="test_star_import",
        target=RewriteTarget(
            file="drivers/postgresdriver.py",
            function="doNewOrder",
            qualified_function="PostgresDriver.doNewOrder",
        ),
        pattern="N_PLUS_ONE_QUERY",
        strategy="N_PLUS_ONE_QUERY_ELIMINATION",
        allowed_regions=["PostgresDriver.doNewOrder"],
    )
    contract.targets = [contract.target]
    result = verify_contract(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(
        v.code == "UNDEFINED_NAME"
        and v.severity == "ERROR"
        and "item_tuple" in v.message
        for v in result.violations
    )


def test_verify_rejects_slow_executemany():
    orig = """
def get_stock(self, rows):
    return None
"""
    opt = """
import psycopg2

def get_stock(self, rows):
    self.cursor.executemany("INSERT INTO STOCK (a, b) VALUES (%s, %s)", rows)
    return None
"""
    result = verify_contract(orig, opt, _static_check_contract())
    assert result.status == "FAIL"
    assert any(
        v.code == "SLOW_EXECUTEMANY" and v.severity == "ERROR"
        for v in result.violations
    )


def test_verify_rejects_executemany_despite_fast_path_in_another_function():
    """The fast path in a sibling function must not exempt the whole file."""
    orig = """
import psycopg2
from psycopg2.extras import execute_batch

def get_stock(self, rows):
    return None

def load_rows(self, row):
    execute_batch(self.cursor, "INSERT INTO LOAD (a) VALUES (%s)", [row])
    return None
"""
    opt = """
import psycopg2
from psycopg2.extras import execute_batch

def get_stock(self, rows):
    self.cursor.executemany("INSERT INTO STOCK (a, b) VALUES (%s, %s)", rows)
    return None

def load_rows(self, row):
    execute_batch(self.cursor, "INSERT INTO LOAD (a) VALUES (%s)", [row])
    return None
"""
    result = verify_contract(orig, opt, _static_check_contract())
    assert result.status == "FAIL"
    assert any(
        v.code == "SLOW_EXECUTEMANY" and v.severity == "ERROR"
        for v in result.violations
    )


def test_preexisting_executemany_not_reported_as_new():
    orig = """
import psycopg2

def get_stock(self, rows):
    return None

def load_rows(self, row):
    self.cursor.executemany("INSERT INTO LOAD (a) VALUES (%s)", [row])
    return None
"""
    opt = """
import psycopg2

def get_stock(self, rows):
    self.cursor.execute("SELECT a FROM STOCK WHERE a = ANY(%s)", (rows,))
    return None

def load_rows(self, row):
    self.cursor.executemany("INSERT INTO LOAD (a) VALUES (%s)", [row])
    return None
"""
    result = verify_contract(orig, opt, _static_check_contract())
    assert not any(v.code == "SLOW_EXECUTEMANY" for v in result.violations)


def test_preexisting_percent_violation_is_deduped():
    orig = """
def get_stock(self, d_id):
    sql = (f"SELECT S_DIST_%02d FROM STOCK WHERE S_W_ID = %s") % (d_id,)
    self.cursor.execute(sql)
    return None
"""
    opt = """
def get_stock(self, d_id):
    sql = (f"SELECT S_DIST_%02d FROM STOCK WHERE S_W_ID = %s") % (d_id,)
    self.cursor.execute(sql)
    row = self.cursor.fetchone()
    return row
"""
    result = verify_contract(orig, opt, _static_check_contract())
    assert not any(v.code == "PERCENT_FORMAT_ARITY" for v in result.violations)


def test_verify_rejects_fragile_composite_agg():
    orig = """
def get_stock(self, item_ids):
    return None
"""
    opt = """
def get_stock(self, item_ids):
    self.cursor.execute("SELECT I_ID, ARRAY_AGG(ROW(I_PRICE, I_NAME)) FROM ITEM WHERE I_ID = ANY(%s)", (item_ids,))
    return None
"""
    result = verify_contract(orig, opt, _static_check_contract())
    assert result.status == "FAIL"
    assert any(
        v.code == "FRAGILE_COMPOSITE_AGG" and v.severity == "ERROR"
        for v in result.violations
    )


def test_verify_rejects_composite_any_array():
    orig = """
def get_stock(self, keys):
    return None
"""
    opt = """
def get_stock(self, keys):
    self.cursor.execute("SELECT S_QUANTITY FROM STOCK WHERE (S_I_ID, S_W_ID) = ANY(%s)", (keys,))
    return None
"""
    result = verify_contract(orig, opt, _static_check_contract())
    assert result.status == "FAIL"
    assert any(
        v.code == "COMPOSITE_ANY_ARRAY" and v.severity == "ERROR"
        for v in result.violations
    )


def test_verify_rejects_lookup_key_not_selected():
    orig = """
def get_stock(self, item_ids):
    return None
"""
    opt = """
def get_stock(self, item_ids):
    self.cursor.execute("SELECT I_PRICE, I_NAME FROM ITEM WHERE I_ID = ANY(%s)", (item_ids,))
    item_rows = self.cursor.fetchall()
    item_map = {row[0]: (row[1], row[2]) for row in item_rows}
    return item_map
"""
    result = verify_contract(orig, opt, _static_check_contract())
    assert result.status == "FAIL"
    assert any(
        v.code == "LOOKUP_KEY_NOT_SELECTED" and v.severity == "ERROR"
        for v in result.violations
    )


def test_preexisting_lookup_key_violation_is_deduped():
    orig = """
def get_stock(self, item_ids):
    self.cursor.execute("SELECT I_PRICE, I_NAME FROM ITEM WHERE I_ID = ANY(%s)", (item_ids,))
    item_rows = self.cursor.fetchall()
    item_map = {row[0]: (row[1], row[2]) for row in item_rows}
    return item_map
"""
    opt = """
def get_stock(self, item_ids):
    extra = 1
    self.cursor.execute("SELECT I_PRICE, I_NAME FROM ITEM WHERE I_ID = ANY(%s)", (item_ids,))
    item_rows = self.cursor.fetchall()
    item_map = {row[0]: (row[1], row[2]) for row in item_rows}
    return item_map
"""
    result = verify_contract(orig, opt, _static_check_contract())
    assert not any(v.code == "LOOKUP_KEY_NOT_SELECTED" for v in result.violations)


BATCHED_DELIVERY_BROKEN = """
TXN_QUERIES = {
    "DELIVERY": {
        "updateOrders": "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s AND O_D_ID = %s AND O_W_ID = %s",
        "updateOrderLine": "UPDATE ORDER_LINE SET OL_DELIVERY_D = %s WHERE OL_O_ID = %s AND OL_D_ID = %s AND OL_W_ID = %s",
    }
}


class Delivery:
    def doDelivery(self, params):
        active_d_ids = params["d_ids"]
        no_o_ids = params["no_o_ids"]
        w_id = params["w_id"]
        o_carrier_id = params["o_carrier_id"]
        ol_delivery_d = params["ol_delivery_d"]

        placeholders = ",".join(["(%s, %s, %s)"] * len(active_d_ids))

        update_orders_params = []
        for no_o_id, d_id in zip(no_o_ids, active_d_ids):
            update_orders_params.extend([o_carrier_id, no_o_id, d_id, w_id])
        self.cursor.execute(
            f"UPDATE ORDERS SET O_CARRIER_ID = %s WHERE (O_ID, O_D_ID, O_W_ID) IN ({placeholders})",
            update_orders_params,
        )

        update_ol_params = []
        for no_o_id, d_id in zip(no_o_ids, active_d_ids):
            update_ol_params.extend([ol_delivery_d, no_o_id, d_id, w_id])
        self.cursor.execute(
            f"UPDATE ORDER_LINE SET OL_DELIVERY_D = %s WHERE (OL_O_ID, OL_D_ID, OL_W_ID) IN ({placeholders})",
            update_ol_params,
        )
        return []
"""

BATCHED_DELIVERY_CORRECT = """
TXN_QUERIES = {
    "DELIVERY": {
        "updateOrders": "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s AND O_D_ID = %s AND O_W_ID = %s",
        "updateOrderLine": "UPDATE ORDER_LINE SET OL_DELIVERY_D = %s WHERE OL_O_ID = %s AND OL_D_ID = %s AND OL_W_ID = %s",
    }
}


class Delivery:
    def doDelivery(self, params):
        active_d_ids = params["d_ids"]
        no_o_ids = params["no_o_ids"]
        w_id = params["w_id"]
        o_carrier_id = params["o_carrier_id"]
        ol_delivery_d = params["ol_delivery_d"]

        placeholders = ",".join(["(%s, %s, %s)"] * len(active_d_ids))

        update_orders_params = [o_carrier_id]
        for no_o_id, d_id in zip(no_o_ids, active_d_ids):
            update_orders_params.extend([no_o_id, d_id, w_id])
        self.cursor.execute(
            f"UPDATE ORDERS SET O_CARRIER_ID = %s WHERE (O_ID, O_D_ID, O_W_ID) IN ({placeholders})",
            update_orders_params,
        )

        update_ol_params = [ol_delivery_d]
        for no_o_id, d_id in zip(no_o_ids, active_d_ids):
            update_ol_params.extend([no_o_id, d_id, w_id])
        self.cursor.execute(
            f"UPDATE ORDER_LINE SET OL_DELIVERY_D = %s WHERE (OL_O_ID, OL_D_ID, OL_W_ID) IN ({placeholders})",
            update_ol_params,
        )
        return []
"""

UNBATCHED_DELIVERY = """
TXN_QUERIES = {
    "DELIVERY": {
        "updateOrders": "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s AND O_D_ID = %s AND O_W_ID = %s",
        "updateOrderLine": "UPDATE ORDER_LINE SET OL_DELIVERY_D = %s WHERE OL_O_ID = %s AND OL_D_ID = %s AND OL_W_ID = %s",
    }
}


class Delivery:
    def doDelivery(self, params):
        active_d_ids = params["d_ids"]
        no_o_ids = params["no_o_ids"]
        w_id = params["w_id"]
        o_carrier_id = params["o_carrier_id"]
        ol_delivery_d = params["ol_delivery_d"]
        for no_o_id, d_id in zip(no_o_ids, active_d_ids):
            self.cursor.execute(
                "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s AND O_D_ID = %s AND O_W_ID = %s",
                [o_carrier_id, no_o_id, d_id, w_id],
            )
            self.cursor.execute(
                "UPDATE ORDER_LINE SET OL_DELIVERY_D = %s WHERE OL_O_ID = %s AND OL_D_ID = %s AND OL_W_ID = %s",
                [ol_delivery_d, no_o_id, d_id, w_id],
            )
        return []
"""


def test_verify_reports_dynamic_placeholder_arity():
    result = verify_contract(
        UNBATCHED_DELIVERY, BATCHED_DELIVERY_BROKEN, _static_check_contract("doDelivery")
    )
    assert result.status == "FAIL"
    arity = [v for v in result.violations if v.code == "DYNAMIC_PLACEHOLDER_ARITY"]
    assert len(arity) == 2
    assert all(v.severity == "ERROR" for v in arity)
    assert all("1 + 3*len(active_d_ids)" in v.message for v in arity)
    assert all("4*len(active_d_ids)" in v.message for v in arity)


def test_verify_accepts_correct_dynamic_placeholder_arity():
    result = verify_contract(
        UNBATCHED_DELIVERY, BATCHED_DELIVERY_CORRECT, _static_check_contract("doDelivery")
    )
    assert not any(v.code == "DYNAMIC_PLACEHOLDER_ARITY" for v in result.violations)


def test_verify_dedupes_preexisting_dynamic_placeholder_arity():
    result = verify_contract(
        BATCHED_DELIVERY_BROKEN, BATCHED_DELIVERY_BROKEN, _static_check_contract("doDelivery")
    )
    assert not any(v.code == "DYNAMIC_PLACEHOLDER_ARITY" for v in result.violations)




DELIVERY_VALUES_ORIGINAL = """
class Delivery:
    def doDelivery(self, params):
        for c_id, d_id in params["customer_keys"]:
            self.cursor.execute(
                "UPDATE CUSTOMER SET C_BALANCE = C_BALANCE + %s "
                "WHERE C_ID = %s AND C_D_ID = %s AND C_W_ID = %s",
                [1.0, c_id, d_id, params["w_id"]],
            )
        return []
"""

DELIVERY_VALUES_BROKEN = """
class Delivery:
    def doDelivery(self, params):
        keys = params["customer_keys"]
        placeholders = ",".join(["(%s, %s, %s)"] * len(keys))
        sql = (
            "UPDATE CUSTOMER AS c SET C_BALANCE = C_BALANCE + v.ol_total "
            "FROM (VALUES " + placeholders + ") AS v(c_id, c_d_id, c_w_id, ol_total) "
            "WHERE c.C_ID = v.c_id"
        )
        self.cursor.execute(sql, [])
        return []
"""

DELIVERY_VALUES_CORRECT = DELIVERY_VALUES_BROKEN.replace(
    '["(%s, %s, %s)"]', '["(%s, %s, %s, %s)"]'
)


def test_verify_reports_values_alias_arity():
    result = verify_contract(
        DELIVERY_VALUES_ORIGINAL,
        DELIVERY_VALUES_BROKEN,
        _static_check_contract("doDelivery"),
    )
    assert result.status == "FAIL"
    arity = [v for v in result.violations if v.code == "VALUES_ALIAS_ARITY"]
    assert len(arity) == 1
    assert arity[0].severity == "ERROR"
    assert "3 expression(s)" in arity[0].message
    assert "4 column(s)" in arity[0].message


def test_verify_accepts_matching_values_alias_arity():
    result = verify_contract(
        DELIVERY_VALUES_ORIGINAL,
        DELIVERY_VALUES_CORRECT,
        _static_check_contract("doDelivery"),
    )
    assert not any(v.code == "VALUES_ALIAS_ARITY" for v in result.violations)


DELIVERY_TUPLE_ORDER_ORIGINAL = """
class Delivery:
    def doDelivery(self, params):
        for d_id, no_o_id in params["orders"]:
            self.cursor.execute(
                "SELECT O_C_ID FROM ORDERS WHERE O_ID = %s AND O_D_ID = %s",
                [no_o_id, d_id],
            )
        return []
"""

DELIVERY_TUPLE_ORDER_BROKEN = """
class Delivery:
    def doDelivery(self, params):
        w_id = params["w_id"]
        valid_orders = []
        for d_id, no_o_id in params["orders"]:
            valid_orders.append((d_id, no_o_id))
        self.cursor.execute(
            "SELECT O_ID, O_D_ID, O_C_ID FROM ORDERS WHERE O_W_ID = %s AND (O_ID, O_D_ID) IN (%s)"
            % ("%s", ",".join(["(%s,%s)" for _ in valid_orders])),
            [w_id] + [val for pair in valid_orders for val in pair],
        )
        return []
"""


def test_verify_reports_composite_tuple_order():
    result = verify_contract(
        DELIVERY_TUPLE_ORDER_ORIGINAL,
        DELIVERY_TUPLE_ORDER_BROKEN,
        _static_check_contract("doDelivery"),
    )
    assert result.status == "FAIL"
    order = [v for v in result.violations if v.code == "COMPOSITE_TUPLE_ORDER"]
    assert len(order) == 1
    assert order[0].severity == "ERROR"
    assert "(d_id, no_o_id)" in order[0].message
    assert "(o_d_id, o_id)" in order[0].message


def test_verify_accepts_correct_composite_tuple_order():
    fixed = DELIVERY_TUPLE_ORDER_BROKEN.replace(
        "valid_orders.append((d_id, no_o_id))",
        "valid_orders.append((no_o_id, d_id))",
    )
    result = verify_contract(
        DELIVERY_TUPLE_ORDER_ORIGINAL,
        fixed,
        _static_check_contract("doDelivery"),
    )
    assert not any(v.code == "COMPOSITE_TUPLE_ORDER" for v in result.violations)


DELIVERY_PREFIX_ORIGINAL = """
class Delivery:
    def doDelivery(self, params):
        self.cursor.execute(
            "UPDATE ORDER_LINE SET OL_DELIVERY_D = %s "
            "WHERE OL_O_ID = %s AND OL_D_ID = %s AND OL_W_ID = %s",
            [params["ol_delivery_d"], params["no_o_id"], params["d_id"], params["w_id"]],
        )
        return []
"""

DELIVERY_PREFIX_BROKEN = """
class Delivery:
    def doDelivery(self, params):
        self.cursor.execute(
            "UPDATE ORDER_LINE SET OL_DELIVERY_D = %s "
            "WHERE O_W_ID = %s AND (OL_O_ID, OL_D_ID) IN (%s)",
            [params["ol_delivery_d"], params["w_id"], params["keys"]],
        )
        return []
"""


def test_verify_reports_table_column_prefix_mismatch():
    result = verify_contract(
        DELIVERY_PREFIX_ORIGINAL,
        DELIVERY_PREFIX_BROKEN,
        _static_check_contract("doDelivery"),
    )
    assert result.status == "FAIL"
    prefix = [v for v in result.violations if v.code == "TABLE_COLUMN_PREFIX_MISMATCH"]
    assert len(prefix) == 1
    assert prefix[0].severity == "ERROR"
    assert "O_W_ID" in prefix[0].message
    assert "OL_" in prefix[0].message


def test_verify_accepts_full_order_line_prefix():
    fixed = DELIVERY_PREFIX_BROKEN.replace("WHERE O_W_ID", "WHERE OL_W_ID")
    result = verify_contract(
        DELIVERY_PREFIX_ORIGINAL,
        fixed,
        _static_check_contract("doDelivery"),
    )
    assert not any(v.code == "TABLE_COLUMN_PREFIX_MISMATCH" for v in result.violations)


ORDER_STATUS_ORIGINAL = """
TXN_QUERIES = {"ORDER_STATUS": {
    "getCustomerByCustomerId": "SELECT C_ID, C_FIRST, C_MIDDLE, C_LAST, C_BALANCE FROM CUSTOMER WHERE C_W_ID = %s AND C_D_ID = %s AND C_ID = %s",
    "getLastOrder": "SELECT O_ID, O_CARRIER_ID, O_ENTRY_D FROM ORDERS WHERE O_W_ID = %s AND O_D_ID = %s AND O_C_ID = %s ORDER BY O_ID DESC LIMIT 1",
    "getOrderLines": "SELECT OL_SUPPLY_W_ID, OL_I_ID, OL_QUANTITY, OL_AMOUNT, OL_DELIVERY_D FROM ORDER_LINE WHERE OL_W_ID = %s AND OL_D_ID = %s AND OL_O_ID = %s",
}}

class Status:
    def doOrderStatus(self, params):
        q = TXN_QUERIES["ORDER_STATUS"]
        c_id = params["c_id"]
        self.cursor.execute(q["getCustomerByCustomerId"], [1, 2, c_id])
        customer = self.cursor.fetchone()
        self.cursor.execute(q["getLastOrder"], [1, 2, c_id])
        order = self.cursor.fetchone()
        if order:
            self.cursor.execute(q["getOrderLines"], [1, 2, order[0]])
            orderLines = self.cursor.fetchall()
        else:
            orderLines = []
        return [customer, order, orderLines]
"""

ORDER_STATUS_WIDENED = ORDER_STATUS_ORIGINAL.replace(
    'q["getCustomerByCustomerId"], [1, 2, c_id]',
    '"SELECT C_ID, C_FIRST, C_MIDDLE, C_LAST, C_BALANCE, C_STREET_1, C_CITY, C_BALANCE, C_DATA FROM CUSTOMER WHERE C_W_ID = %s AND C_D_ID = %s AND C_ID = %s", [1, 2, c_id]',
)


def test_verify_reports_returned_row_width_changed():
    result = verify_contract(
        ORDER_STATUS_ORIGINAL,
        ORDER_STATUS_WIDENED,
        _static_check_contract("doOrderStatus"),
    )
    assert result.status == "FAIL"
    width = [v for v in result.violations if v.code == "RETURNED_ROW_WIDTH_CHANGED"]
    assert len(width) == 1
    assert width[0].severity == "ERROR"
    assert "'customer'" in width[0].message
    assert "original returned [5]" in width[0].message


def test_verify_accepts_unchanged_returned_row_width():
    result = verify_contract(
        ORDER_STATUS_ORIGINAL,
        ORDER_STATUS_ORIGINAL,
        _static_check_contract("doOrderStatus"),
    )
    assert not any(v.code == "RETURNED_ROW_WIDTH_CHANGED" for v in result.violations)


DELIVERY_ALIAS_ORIGINAL = """
class Delivery:
    def doDelivery(self, params):
        self.cursor.execute(
            "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s AND O_D_ID = %s AND O_W_ID = %s",
            [params["carrier"], params["no_o_id"], params["d_id"], params["w_id"]],
        )
        return []
"""

DELIVERY_ALIAS_BROKEN = """
class Delivery:
    def doDelivery(self, params):
        keys = params["keys"]
        groups = ",".join(["(%s, %s, %s, %s)"] * len(keys))
        flat = [val for tup in keys for val in tup]
        self.cursor.execute(
            f"UPDATE ORDERS SET O_CARRIER_ID = v.carrier_id "
            f"FROM (VALUES {groups}) AS v(carrier_id, o_id, d_id, w_id) "
            f"WHERE O_ID = v.o_id AND O_D_ID = v.d_id AND O_W_ID = v.w_id",
            flat,
        )
        return []
"""


def test_verify_reports_ambiguous_values_alias():
    result = verify_contract(
        DELIVERY_ALIAS_ORIGINAL,
        DELIVERY_ALIAS_BROKEN,
        _static_check_contract("doDelivery"),
    )
    assert result.status == "FAIL"
    alias = [v for v in result.violations if v.code == "AMBIGUOUS_VALUES_ALIAS"]
    assert len(alias) == 1
    assert alias[0].severity == "ERROR"
    assert "o_id" in alias[0].message
    assert "ambiguous" in alias[0].message


def test_verify_accepts_qualified_values_alias():
    fixed = DELIVERY_ALIAS_BROKEN.replace("WHERE O_ID =", "WHERE ORDERS.O_ID =").replace(
        "AND O_D_ID =", "AND ORDERS.O_D_ID ="
    ).replace("AND O_W_ID =", "AND ORDERS.O_W_ID =")
    result = verify_contract(
        DELIVERY_ALIAS_ORIGINAL,
        fixed,
        _static_check_contract("doDelivery"),
    )
    assert not any(v.code == "AMBIGUOUS_VALUES_ALIAS" for v in result.violations)


PAYMENT_CTE_ORIGINAL = """
class Payment:
    def doPayment(self, params):
        self.cursor.execute(
            "UPDATE WAREHOUSE SET W_YTD = W_YTD + %s WHERE W_ID = %s",
            [params["h_amount"], params["w_id"]],
        )
        return []
"""

PAYMENT_CTE_BROKEN = """
class Payment:
    def doPayment(self, params):
        self.cursor.execute(
            "WITH u1 AS (UPDATE WAREHOUSE SET W_YTD = W_YTD + %s WHERE W_ID = %s RETURNING 1), "
            "u3 AS (%s RETURNING 1) INSERT INTO HISTORY VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [params["h_amount"], params["w_id"], params["update_sql"],
             params["c_id"], params["c_d_id"], params["c_w_id"], params["d_id"],
             params["w_id"], params["h_date"], params["h_amount"], params["h_data"]],
        )
        return []
"""


def test_verify_reports_statement_as_parameter():
    result = verify_contract(
        PAYMENT_CTE_ORIGINAL,
        PAYMENT_CTE_BROKEN,
        _static_check_contract("doPayment"),
    )
    assert result.status == "FAIL"
    stmt = [v for v in result.violations if v.code == "STATEMENT_AS_PARAMETER"]
    assert len(stmt) == 1
    assert stmt[0].severity == "ERROR"
    assert "AS (%s RETURNING" in stmt[0].message


def test_verify_accepts_inlined_cte_statement():
    fixed = PAYMENT_CTE_BROKEN.replace(
        "u3 AS (%s RETURNING 1)",
        "u3 AS (UPDATE CUSTOMER SET C_BALANCE = %s WHERE C_ID = %s RETURNING 1)",
    ).replace(
        'params["update_sql"],',
        'params["c_balance"], params["c_id"],',
    )
    result = verify_contract(
        PAYMENT_CTE_ORIGINAL,
        fixed,
        _static_check_contract("doPayment"),
    )
    assert not any(v.code == "STATEMENT_AS_PARAMETER" for v in result.violations)


PAYMENT_ROW_ORIGINAL = """
class Payment:
    def doPayment(self, params):
        c_id = params["c_id"]
        if c_id is not None:
            customer = self.cursor.fetchone()
        else:
            customer = self.cursor.fetchall()[0]
            c_id = customer[0]
        return [customer]
"""

PAYMENT_ROW_BROKEN = """
class Payment:
    def doPayment(self, params):
        c_id = params["c_id"]
        if c_id is not None:
            row = self.cursor.fetchone()
            customer = row[:18]
        else:
            customer = self.cursor.fetchall()[0]
            c_id = customer[0]
        if customer and len(row) > 0:
            pass
        return [customer]
"""


def test_verify_reports_possibly_unbound_local():
    result = verify_contract(
        PAYMENT_ROW_ORIGINAL,
        PAYMENT_ROW_BROKEN,
        _static_check_contract("doPayment"),
    )
    assert result.status == "FAIL"
    unbound = [v for v in result.violations if v.code == "POSSIBLY_UNBOUND_LOCAL"]
    assert len(unbound) == 1
    assert unbound[0].severity == "ERROR"
    assert "'row'" in unbound[0].message
    assert "UnboundLocalError" in unbound[0].message


def test_verify_accepts_all_paths_bound():
    fixed = PAYMENT_ROW_BROKEN.replace(
        '            c_id = customer[0]\n',
        '            c_id = customer[0]\n            row = ()\n',
    )
    result = verify_contract(
        PAYMENT_ROW_ORIGINAL,
        fixed,
        _static_check_contract("doPayment"),
    )
    assert not any(v.code == "POSSIBLY_UNBOUND_LOCAL" for v in result.violations)


DELIVERY_MOGRIFY_ORIGINAL = """
class Delivery:
    def doDelivery(self, params):
        self.cursor.execute(
            "DELETE FROM NEW_ORDER WHERE NO_D_ID = %s AND NO_W_ID = %s AND NO_O_ID = %s",
            [params["d_id"], params["w_id"], params["no_o_id"]],
        )
        return []
"""

DELIVERY_MOGRIFY_BROKEN = """
class Delivery:
    def doDelivery(self, params):
        delete_params = params["deletes"]
        args_str_del = ",".join(
            self.cursor.mogrify("(%s, %s, %s)", p).decode("utf-8")
            for p in delete_params
        )
        flat_del_params = [val for p in delete_params for val in p]
        self.cursor.execute(
            f"DELETE FROM NEW_ORDER WHERE (NO_D_ID, NO_W_ID, NO_O_ID) IN ({args_str_del})",
            flat_del_params,
        )
        return []
"""


def test_verify_reports_mogrify_with_params():
    result = verify_contract(
        DELIVERY_MOGRIFY_ORIGINAL,
        DELIVERY_MOGRIFY_BROKEN,
        _static_check_contract("doDelivery"),
    )
    assert result.status == "FAIL"
    mogrify = [v for v in result.violations if v.code == "MOGRIFY_WITH_PARAMS"]
    assert len(mogrify) == 1
    assert mogrify[0].severity == "ERROR"
    assert "not all arguments converted" in mogrify[0].message


DELIVERY_EMPTY_PARAMS_ORIGINAL = """
class Delivery:
    def doDelivery(self, params):
        self.cursor.execute(
            "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s AND O_D_ID = %s AND O_W_ID = %s",
            [params["carrier"], params["no_o_id"], params["d_id"], params["w_id"]],
        )
        return []
"""

DELIVERY_EMPTY_PARAMS_BROKEN = """
class Delivery:
    def doDelivery(self, params):
        order_tuples = params["orders"]
        placeholders = ",".join(["(%s, %s, %s)"] * len(order_tuples))
        self.cursor.execute(
            f"UPDATE ORDERS SET O_CARRIER_ID = %s WHERE (O_ID, O_D_ID, O_W_ID) IN ({placeholders})",
            [params["carrier"]] * len(order_tuples) if False else [],
        )
        return []
"""


def test_verify_reports_empty_params_with_placeholders():
    result = verify_contract(
        DELIVERY_EMPTY_PARAMS_ORIGINAL,
        DELIVERY_EMPTY_PARAMS_BROKEN,
        _static_check_contract("doDelivery"),
    )
    assert result.status == "FAIL"
    empty = [v for v in result.violations if v.code == "EMPTY_PARAMS_WITH_PLACEHOLDERS"]
    assert len(empty) == 1
    assert empty[0].severity == "ERROR"
    assert "list index out of range" in empty[0].message


DELIVERY_DYNAMIC_PREFIX_ORIGINAL = """
class Delivery:
    def doDelivery(self, params):
        self.cursor.execute(
            "SELECT OL_D_ID, OL_O_ID, SUM(OL_AMOUNT) FROM ORDER_LINE "
            "WHERE OL_W_ID = %s AND OL_D_ID = %s AND OL_O_ID = %s",
            [params["w_id"], params["d_id"], params["no_o_id"]],
        )
        return []
"""

DELIVERY_DYNAMIC_PREFIX_BROKEN = """
class Delivery:
    def doDelivery(self, params):
        order_keys = params["keys"]
        placeholders = " OR ".join(["(O_D_ID = %s AND O_ID = %s)"] * len(order_keys))
        self.cursor.execute(
            f"SELECT SUM(OL_AMOUNT) FROM ORDER_LINE WHERE OL_W_ID = %s AND ({placeholders})",
            params["params"],
        )
        return []
"""


def test_verify_reports_dynamic_foreign_prefix():
    result = verify_contract(
        DELIVERY_DYNAMIC_PREFIX_ORIGINAL,
        DELIVERY_DYNAMIC_PREFIX_BROKEN,
        _static_check_contract("doDelivery"),
    )
    assert result.status == "FAIL"
    prefix = [v for v in result.violations if v.code == "TABLE_COLUMN_PREFIX_MISMATCH"]
    assert len(prefix) == 2
    assert all(v.severity == "ERROR" for v in prefix)


DELIVERY_BATCH_ORDER_ORIGINAL = """
class Delivery:
    def doDelivery(self, params):
        for d_id, no_o_id in params["orders"]:
            self.cursor.execute(
                "DELETE FROM NEW_ORDER WHERE NO_D_ID = %s AND NO_W_ID = %s AND NO_O_ID = %s",
                [d_id, params["w_id"], no_o_id],
            )
        return []
"""

DELIVERY_BATCH_ORDER_BROKEN = """
TXN_QUERIES = {"DELIVERY": {
    "deleteNewOrder": "DELETE FROM NEW_ORDER WHERE NO_D_ID = %s AND NO_W_ID = %s AND NO_O_ID = %s",
}}
class Delivery:
    def doDelivery(self, params):
        q = TXN_QUERIES["DELIVERY"]
        order_tuples = [(no_o_id, d_id, params["w_id"]) for d_id, no_o_id in params["orders"]]
        self.cursor.execute_batch(self.cursor, q["deleteNewOrder"], order_tuples)
        return []
"""


def test_verify_reports_batched_write_tuple_order():
    result = verify_contract(
        DELIVERY_BATCH_ORDER_ORIGINAL,
        DELIVERY_BATCH_ORDER_BROKEN,
        _static_check_contract("doDelivery"),
    )
    assert result.status == "FAIL"
    order = [v for v in result.violations if v.code == "BATCHED_WRITE_TUPLE_ORDER"]
    assert len(order) == 1
    assert order[0].severity == "ERROR"
    assert "NO_D_ID" in order[0].message or "no_d_id" in order[0].message


DELIVERY_WRITE_ORIGINAL = """
class Delivery:
    def doDelivery(self, params):
        self.cursor.execute(
            "DELETE FROM NEW_ORDER WHERE NO_D_ID = %s AND NO_W_ID = %s AND NO_O_ID = %s",
            [params["d"], params["w"], params["o"]],
        )
        self.cursor.execute(
            "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s",
            [params["c"], params["o"]],
        )
        return []
"""

DELIVERY_WRITE_DROPPED = """
class Delivery:
    def doDelivery(self, params):
        self.cursor.execute(
            "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s",
            [params["c"], params["o"]],
        )
        return []
"""

ORDER_LIMIT_ORIGINAL = """
class Status:
    def doOrderStatus(self, params):
        self.cursor.execute(
            "SELECT O_ID, O_CARRIER_ID, O_ENTRY_D FROM ORDERS WHERE O_W_ID = %s ORDER BY O_ID DESC LIMIT 1",
            [params["w"]],
        )
        order = self.cursor.fetchone()
        return [order]
"""

ORDER_LIMIT_DROPPED = """
class Status:
    def doOrderStatus(self, params):
        self.cursor.execute(
            "SELECT O_ID, O_CARRIER_ID, O_ENTRY_D FROM ORDERS WHERE O_W_ID = %s",
            [params["w"]],
        )
        order = self.cursor.fetchone()
        return [order]
"""


def test_verify_reports_dropped_write_table():
    result = verify_contract(
        DELIVERY_WRITE_ORIGINAL,
        DELIVERY_WRITE_DROPPED,
        _static_check_contract("doDelivery"),
    )
    assert result.status == "FAIL"
    writes = [v for v in result.violations if v.code == "WRITE_SET_CHANGED"]
    assert len(writes) == 1
    assert writes[0].severity == "ERROR"
    assert "DELETE NEW_ORDER" in writes[0].message


def test_verify_reports_dropped_order_by_limit():
    result = verify_contract(
        ORDER_LIMIT_ORIGINAL,
        ORDER_LIMIT_DROPPED,
        _static_check_contract("doOrderStatus"),
    )
    assert result.status == "FAIL"
    shapes = [v for v in result.violations if v.code == "RETURN_ORDER_CHANGED"]
    assert len(shapes) == 2
    assert all(v.severity == "ERROR" for v in shapes)


DELIVERY_WRONG_COLUMN_ORIGINAL = """
TXN_QUERIES = {"DELIVERY": {
    "getCId": "SELECT O_C_ID FROM ORDERS WHERE O_ID = %s AND O_D_ID = %s AND O_W_ID = %s",
    "sumOLAmount": "SELECT SUM(OL_AMOUNT) FROM ORDER_LINE WHERE OL_O_ID = %s AND OL_D_ID = %s AND OL_W_ID = %s",
}}
class Delivery:
    def doDelivery(self, params):
        q = TXN_QUERIES["DELIVERY"]
        self.cursor.execute(q["getCId"], [params["o"], params["d"], params["w"]])
        self.cursor.execute(q["sumOLAmount"], [params["o"], params["d"], params["w"]])
        return []
"""

DELIVERY_WRONG_COLUMN_BROKEN = """
class Delivery:
    def doDelivery(self, params):
        order_keys = params["keys"]
        placeholders = " OR ".join(["(O_D_ID = %s AND O_ID = %s)"] * len(order_keys))
        self.cursor.execute(
            f"SELECT SUM(OL_AMOUNT) FROM ORDER_LINE WHERE OL_W_ID = %s AND ({placeholders})",
            params["p"],
        )
        return []
"""


def test_verify_reports_wrong_table_column():
    result = verify_contract(
        DELIVERY_WRONG_COLUMN_ORIGINAL,
        DELIVERY_WRONG_COLUMN_BROKEN,
        _static_check_contract("doDelivery"),
    )
    assert result.status == "FAIL"
    wrong = [v for v in result.violations if v.code == "WRONG_TABLE_COLUMN"]
    assert len(wrong) == 2
    assert all(v.severity == "ERROR" for v in wrong)
    assert all("ORDER_LINE" in v.message for v in wrong)


ORDER_JOIN_ORIGINAL = """
class Status:
    def doOrderStatus(self, params):
        self.cursor.execute(
            "SELECT O_ID, O_CARRIER_ID, O_ENTRY_D FROM ORDERS WHERE O_W_ID = %s ORDER BY O_ID DESC LIMIT 1",
            [params["w"]],
        )
        order = self.cursor.fetchone()
        if order:
            self.cursor.execute(
                "SELECT OL_SUPPLY_W_ID, OL_I_ID FROM ORDER_LINE WHERE OL_O_ID = %s",
                [order[0]],
            )
            orderLines = self.cursor.fetchall()
        else:
            orderLines = []
        return [order, orderLines]
"""

ORDER_JOIN_COLLAPSED = """
class Status:
    def doOrderStatus(self, params):
        self.cursor.execute(
            "SELECT O.O_ID, O.O_CARRIER_ID, OL.OL_I_ID FROM ORDERS O "
            "JOIN ORDER_LINE OL ON OL.OL_O_ID = O.O_ID "
            "WHERE O.O_W_ID = %s ORDER BY O.O_ID DESC LIMIT 1",
            [params["w"]],
        )
        rows = self.cursor.fetchall()
        if rows:
            order = (rows[0][0], rows[0][1])
            orderLines = [(r[2],) for r in rows]
        else:
            order = None
            orderLines = []
        return [order, orderLines]
"""


def test_verify_reports_join_limit_collapse():
    result = verify_contract(
        ORDER_JOIN_ORIGINAL,
        ORDER_JOIN_COLLAPSED,
        _static_check_contract("doOrderStatus"),
    )
    assert result.status == "FAIL"
    collapse = [v for v in result.violations if v.code == "RETURN_JOIN_LIMIT_COLLAPSE"]
    assert len(collapse) == 2
    assert all(v.severity == "ERROR" for v in collapse)


def test_verify_accepts_lateral_join_without_top_limit():
    lateral = ORDER_JOIN_COLLAPSED.replace(
        '"JOIN ORDER_LINE OL ON OL.OL_O_ID = O.O_ID "',
        '"LEFT JOIN LATERAL (SELECT OL_I_ID FROM ORDER_LINE WHERE OL_O_ID = O.O_ID) OL ON TRUE "',
    ).replace(
        '"WHERE O.O_W_ID = %s ORDER BY O.O_ID DESC LIMIT 1"',
        '"WHERE O.O_W_ID = %s ORDER BY O.O_ID DESC"',
    )
    result = verify_contract(
        ORDER_JOIN_ORIGINAL,
        lateral,
        _static_check_contract("doOrderStatus"),
    )
    assert not any(v.code == "RETURN_JOIN_LIMIT_COLLAPSE" for v in result.violations)


DELIVERY_NARROW_PICK_ORIGINAL = """
TXN_QUERIES = {"DELIVERY": {
    "getNewOrder": "SELECT NO_O_ID FROM NEW_ORDER WHERE NO_D_ID = %s AND NO_W_ID = %s AND NO_O_ID > -1 LIMIT 1",
    "deleteNewOrder": "DELETE FROM NEW_ORDER WHERE NO_D_ID = %s AND NO_W_ID = %s AND NO_O_ID = %s",
}}
class Delivery:
    def doDelivery(self, params):
        q = TXN_QUERIES["DELIVERY"]
        for d_id in params["d_ids"]:
            self.cursor.execute(q["getNewOrder"], [d_id, params["w_id"]])
            no_o_id = self.cursor.fetchone()[0]
            self.cursor.execute(q["deleteNewOrder"], [d_id, params["w_id"], no_o_id])
        return []
"""

DELIVERY_NARROW_PICK_BROKEN = """
class Delivery:
    def doDelivery(self, params):
        w_id = params["w_id"]
        self.cursor.execute(
            "SELECT NO_D_ID, NO_O_ID FROM NEW_ORDER WHERE NO_W_ID = %s AND NO_O_ID > -1",
            [w_id],
        )
        new_order_map = {row[0]: row[1] for row in self.cursor.fetchall()}
        for d_id in params["d_ids"]:
            no_o_id = new_order_map[d_id]
            self.cursor.execute(
                "DELETE FROM NEW_ORDER WHERE NO_D_ID = %s AND NO_W_ID = %s AND NO_O_ID = %s",
                [d_id, w_id, no_o_id],
            )
        return []
"""


def test_verify_reports_narrow_key_pick():
    result = verify_contract(
        DELIVERY_NARROW_PICK_ORIGINAL,
        DELIVERY_NARROW_PICK_BROKEN,
        _static_check_contract("doDelivery"),
    )
    assert result.status == "FAIL"
    picks = [v for v in result.violations if v.code == "NARROW_KEY_PICK"]
    assert len(picks) == 1
    assert picks[0].severity == "ERROR"
    assert "NO_O_ID" in picks[0].message


def test_verify_reports_aggregate_reduction_dropped():
    orig = """
class Delivery:
    def doDelivery(self, params):
        self.cursor.execute(
            "SELECT NO_D_ID, MIN(NO_O_ID) FROM NEW_ORDER WHERE NO_W_ID = %s GROUP BY NO_D_ID",
            [params["w_id"]],
        )
        return self.cursor.fetchall()
"""
    bad = DELIVERY_NARROW_PICK_BROKEN
    result = verify_contract(orig, bad, _static_check_contract("doDelivery"))
    assert result.status == "FAIL"
    agg = [v for v in result.violations if v.code == "AGGREGATE_REDUCTION_DROPPED"]
    assert len(agg) == 1
    assert agg[0].severity == "ERROR"
