from src.code_rewriter.tools.sql_analysis import (
    duplicate_column_predicate_sql,
    duplicate_where_sql,
    find_select_column_count,
    implicit_join_sql,
    multi_statement_sql,
    placeholder_param_mismatch_sql,
    planner_unfriendly_sql,
    row_index_violations,
    unknown_query_key_sql,
)

STOCK_SELECT_6 = (
    "SELECT S_QUANTITY, S_YTD, S_ORDER_CNT, S_REMOTE_CNT, S_DIST_%02d, S_DATA "
    "FROM STOCK WHERE S_I_ID = %s AND S_W_ID = %s"
)
STOCK_SELECT_8 = (
    "SELECT S_I_ID, S_W_ID, S_QUANTITY, S_YTD, S_ORDER_CNT, S_REMOTE_CNT, "
    "S_DIST_%02d, S_DATA FROM STOCK WHERE S_I_ID = %s AND S_W_ID = %s"
)


def _motivation_source(select_columns: str, row_indices: str) -> str:
    return f'''
TXN_QUERIES = {{
    "NEW_ORDER": {{
        "getStockInfo": "{select_columns}"
    }}
}}


class OrderService:
    def process_new_order(self, d_id, stock_params):
        q = TXN_QUERIES["NEW_ORDER"]
        stock_placeholders = ",".join(["?"] * len(stock_params))
        flat_stock_params = stock_params
        stock_sql = (q["getStockInfo"] % (d_id)).replace("WHERE S_I_ID = %s AND S_W_ID = %s", "WHERE (S_I_ID, S_W_ID) IN (" + stock_placeholders + ")")
        self.cursor.execute(stock_sql, flat_stock_params)
        stock_map = {{(row[0], row[1]): ({row_indices}) for row in self.cursor.fetchall()}}
        return stock_map
'''


def test_resolve_dynamic_sql_with_replace():
    source = _motivation_source(STOCK_SELECT_6, "row[2], row[3], row[4], row[5], row[6], row[7]")
    violations = row_index_violations(source)
    assert len(violations) == 1
    assert find_select_column_count(violations[0]["sql"]) == 6


def test_row_index_out_of_range_detected():
    source = _motivation_source(STOCK_SELECT_6, "row[2], row[3], row[4], row[5], row[6], row[7]")
    violations = row_index_violations(source)
    assert len(violations) == 1
    assert violations[0]["column_count"] == 6
    assert violations[0]["max_index"] == 7
    assert violations[0]["function"] == "OrderService.process_new_order"


def test_row_index_ok_when_key_columns_selected():
    source = _motivation_source(STOCK_SELECT_8, "row[2], row[3], row[4], row[5], row[6], row[7]")
    assert row_index_violations(source) == []


def test_scalar_subquery_not_flagged_as_planner_unfriendly():
    source = '''
def get_stock(cursor, item_id):
    sql = "SELECT S_QUANTITY FROM STOCK WHERE S_I_ID = (SELECT S_I_ID FROM ORDER_LINE WHERE OL_I_ID = %s)"
    cursor.execute(sql, (item_id,))
'''
    assert planner_unfriendly_sql(source) == []


def test_cross_join_derived_table_flagged():
    source = '''
def get_stock(cursor, item_id):
    sql = "SELECT S_QUANTITY FROM ORDER_LINE, STOCK, (SELECT S_I_ID FROM ORDER_LINE) d WHERE S_I_ID = %s"
    cursor.execute(sql, (item_id,))
'''
    violations = planner_unfriendly_sql(source)
    assert len(violations) == 1
    assert violations[0]["function"] == "get_stock"
    assert "FROM" in violations[0]["sql"]


def test_select_star_skipped():
    source = '''
def get_stock(cursor, item_id):
    cursor.execute("SELECT * FROM STOCK WHERE S_I_ID = %s", (item_id,))
    row = cursor.fetchone()
    return row[3]
'''
    assert row_index_violations(source) == []


def test_multi_statement_execute_flagged():
    source = '''
def load(cursor, x):
    cursor.execute("SELECT a FROM t; SELECT b FROM u;", (x,))
'''
    violations = multi_statement_sql(source)
    assert len(violations) == 1
    assert violations[0]["statements"] == 2
    assert violations[0]["function"] == "load"

    single = '''
def load(cursor, x):
    cursor.execute("SELECT a FROM t WHERE id = ?", (x,))
'''
    assert multi_statement_sql(single) == []

    trailing = '''
def load(cursor, x):
    cursor.execute("SELECT a FROM t;", (x,))
'''
    assert multi_statement_sql(trailing) == []


def test_duplicate_where_flagged():
    source = '''
def get_items(cursor, ids):
    sql = "SELECT I_PRICE, I_NAME, I_DATA FROM ITEM WHERE I_ID = %s" + " WHERE i_id IN (%s)"
    cursor.execute(sql, ids)
'''
    violations = duplicate_where_sql(source)
    assert len(violations) == 1
    assert violations[0]["where_count"] == 2
    assert violations[0]["function"] == "get_items"

    subquery = '''
def get_items(cursor, x):
    cursor.execute("SELECT a FROM t WHERE a IN (SELECT b FROM u WHERE c = %s)", (x,))
'''
    assert duplicate_where_sql(subquery) == []


def test_unknown_query_key_flagged():
    source = '''
TXN_QUERIES = {"DELIVERY": {"getNewOrder": "SELECT NO_O_ID FROM NEW_ORDER WHERE NO_D_ID = %s"}}

def do_delivery(cursor, w_id):
    q = TXN_QUERIES["DELIVERY"]
    cursor.execute(q["getNewOrderAll"], [w_id])
'''
    violations = unknown_query_key_sql(source)
    assert len(violations) == 1
    assert violations[0]["key"] == "getNewOrderAll"
    assert violations[0]["function"] == "do_delivery"


def test_unknown_query_key_ok_when_key_exists():
    source = '''
TXN_QUERIES = {"DELIVERY": {"getNewOrder": "SELECT NO_O_ID FROM NEW_ORDER WHERE NO_D_ID = %s"}}

def do_delivery(cursor, w_id):
    q = TXN_QUERIES["DELIVERY"]
    cursor.execute(q["getNewOrder"], [w_id])
'''
    assert unknown_query_key_sql(source) == []


def test_unknown_query_key_ignores_unresolvable_base():
    source = '''
def do_delivery(cursor, w_id):
    d = {}
    cursor.execute(d["missingKey"], [w_id])
'''
    assert unknown_query_key_sql(source) == []


def test_implicit_join_three_relations_flagged():
    source = '''
def stock_level(cursor, w_id, d_id):
    cursor.execute(
        "SELECT COUNT(DISTINCT OL_I_ID) FROM ORDER_LINE, STOCK, DISTRICT "
        "WHERE OL_W_ID = D_W_ID AND S_I_ID = OL_I_ID",
        (w_id, d_id),
    )
'''
    violations = implicit_join_sql(source)
    assert len(violations) == 1
    assert violations[0]["function"] == "stock_level"
    assert violations[0]["relations"] == 3


def test_implicit_join_two_relations_not_flagged():
    source = '''
def lookup(cursor, x):
    cursor.execute("SELECT a FROM t, u WHERE t.id = u.id", (x,))
'''
    assert implicit_join_sql(source) == []


def test_explicit_join_not_flagged():
    source = '''
def lookup(cursor, x):
    cursor.execute("SELECT a FROM t JOIN u ON t.id = u.id JOIN v ON u.id = v.id", (x,))
'''
    assert implicit_join_sql(source) == []


def test_derived_table_two_relations_not_flagged():
    source = '''
def lookup(cursor, x):
    cursor.execute("SELECT a FROM (SELECT a, b FROM t) d, u WHERE d.a = u.a", (x,))
'''
    assert implicit_join_sql(source) == []


def test_subquery_comma_inside_parens_not_flagged():
    source = '''
def lookup(cursor, x):
    cursor.execute("SELECT a FROM t WHERE a IN (SELECT b FROM u, v WHERE u.id = v.id)", (x,))
'''
    assert implicit_join_sql(source) == []


def test_placeholder_param_mismatch_flagged():
    source = '''
def f(cursor, x):
    cursor.execute("SELECT a FROM t WHERE a = %s AND b = %s", (x,))
'''
    violations = placeholder_param_mismatch_sql(source)
    assert len(violations) == 1
    assert violations[0]["placeholders"] == 2
    assert violations[0]["params"] == 1
    assert violations[0]["function"] == "f"


def test_placeholder_param_match_not_flagged():
    source = '''
def f(cursor, x, y):
    cursor.execute("SELECT a FROM t WHERE a = %s AND b = %s", (x, y))
'''
    assert placeholder_param_mismatch_sql(source) == []


def test_placeholder_param_executemany_not_flagged():
    source = '''
def f(cursor, rows):
    cursor.executemany("INSERT INTO t (a, b) VALUES (%s, %s)", rows)
'''
    assert placeholder_param_mismatch_sql(source) == []


def test_placeholder_param_dynamic_params_not_flagged():
    source = '''
def f(cursor, x, ids):
    cursor.execute("SELECT a FROM t WHERE a = %s AND b IN (%s)", [x] + ids)
'''
    assert placeholder_param_mismatch_sql(source) == []


def test_placeholder_param_fstring_not_flagged():
    source = '''
def f(cursor, x):
    cursor.execute(f"SELECT a FROM t WHERE a = {x}", (x,))
'''
    assert placeholder_param_mismatch_sql(source) == []


def test_placeholder_param_mismatch_syntax_error():
    assert placeholder_param_mismatch_sql("def f(:") == []


def test_duplicate_column_predicate_flagged():
    source = '''
def get_items(cursor, ids):
    sql = "SELECT a FROM t WHERE id = %s" + " AND id IN (%s)"
    cursor.execute(sql, ids)
'''
    violations = duplicate_column_predicate_sql(source)
    assert len(violations) == 1
    assert violations[0]["column"] == "id"
    assert violations[0]["function"] == "get_items"


def test_duplicate_column_different_columns_not_flagged():
    source = '''
def get_items(cursor, a, bs):
    cursor.execute("SELECT v FROM t WHERE A = %s AND B IN (%s)", (a, bs))
'''
    assert duplicate_column_predicate_sql(source) == []


def test_duplicate_column_any_not_flagged():
    source = '''
def get_items(cursor, ids):
    cursor.execute("SELECT v FROM t WHERE id = ANY(%s)", (ids,))
'''
    assert duplicate_column_predicate_sql(source) == []


def test_duplicate_column_syntax_error():
    assert duplicate_column_predicate_sql("def f(:") == []

