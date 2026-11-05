from pathlib import Path

import pytest

from src.code_rewriter.tools.sql_analysis import (
    composite_any_array_sql,
    duplicate_column_predicate_sql,
    duplicate_where_sql,
    dynamic_placeholder_arity_sql,
    find_select_column_count,
    find_select_columns,
    fragile_composite_agg_sql,
    implicit_join_sql,
    lookup_key_not_selected_sql,
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


def test_placeholder_param_fstring_variable_not_flagged():
    source = '''
def doStockLevel(self, params):
    sub_query = "SELECT O_ID FROM ORDER WHERE O_W_ID = %s AND O_D_ID = %s"
    combined_sql = f"""
        SELECT COUNT(DISTINCT(OL_I_ID)) FROM ORDER_LINE, STOCK
        WHERE OL_W_ID = %s
          AND OL_D_ID = %s
          AND OL_O_ID < ({sub_query})
          AND OL_O_ID >= (({sub_query}) - 20)
          AND S_W_ID = %s
          AND S_QUANTITY < %s
    """
    self.cursor.execute(
        combined_sql, [1, 2, 3, 4, 5, 6, 7, 8]
    )
'''
    assert placeholder_param_mismatch_sql(source) == []


def test_placeholder_param_variable_constant_still_flagged():
    source = '''
def f(cursor, x):
    sql = "SELECT a FROM t WHERE a = %s AND b = %s"
    cursor.execute(sql, (x,))
'''
    violations = placeholder_param_mismatch_sql(source)
    assert len(violations) == 1
    assert violations[0]["placeholders"] == 2
    assert violations[0]["params"] == 1


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


def test_find_select_columns_basic():
    sql = "SELECT S_QUANTITY, S_YTD, S_ORDER_CNT FROM STOCK WHERE S_I_ID = %s"
    assert find_select_columns(sql) == ["s_quantity", "s_ytd", "s_order_cnt"]


def test_find_select_columns_alias_and_quoting():
    sql = 'SELECT t."S_I_ID" AS item, "S_W_ID" qty, SUM(OL_AMOUNT) FROM STOCK'
    assert find_select_columns(sql) == ["s_i_id", "s_w_id", "sum(ol_amount)"]


def test_find_select_columns_star_returns_none():
    assert find_select_columns("SELECT * FROM STOCK WHERE S_I_ID = %s") is None
    assert find_select_columns("SELECT t.* FROM STOCK t") is None


def test_find_select_columns_no_from_returns_none():
    assert find_select_columns("SELECT 1") is None


def test_fragile_composite_agg_flagged():
    source = '''
def get_items(cursor, ids):
    cursor.execute("SELECT I_ID, ARRAY_AGG(ROW(I_PRICE, I_NAME)) FROM ITEM WHERE I_ID = ANY(%s)", (ids,))
'''
    violations = fragile_composite_agg_sql(source)
    assert len(violations) == 1
    assert violations[0]["function"] == "get_items"
    assert "ARRAY_AGG" in violations[0]["sql"]


def test_fragile_composite_agg_plain_not_flagged():
    source = '''
def get_items(cursor, ids):
    cursor.execute("SELECT I_ID, ARRAY_AGG(OL_AMOUNT) FROM ORDER_LINE WHERE OL_I_ID = ANY(%s)", (ids,))
'''
    assert fragile_composite_agg_sql(source) == []


def test_composite_any_array_flagged():
    source = '''
def get_stock(cursor, keys):
    cursor.execute("SELECT S_QUANTITY FROM STOCK WHERE (S_I_ID, S_W_ID) = ANY(%s)", (keys,))
'''
    violations = composite_any_array_sql(source)
    assert len(violations) == 1
    assert violations[0]["function"] == "get_stock"


def test_composite_in_flattened_not_flagged():
    source = '''
def get_stock(cursor, keys):
    cursor.execute("SELECT S_QUANTITY FROM STOCK WHERE (S_I_ID, S_W_ID) IN ((%s, %s), (%s, %s))", keys)
'''
    assert composite_any_array_sql(source) == []


def test_scalar_any_not_flagged_as_composite():
    source = '''
def get_stock(cursor, ids):
    cursor.execute("SELECT S_QUANTITY FROM STOCK WHERE S_I_ID = ANY(%s)", (ids,))
'''
    assert composite_any_array_sql(source) == []


def test_lookup_key_not_selected_flagged():
    source = '''
def doNewOrder(self, i_ids):
    self.cursor.execute("SELECT I_PRICE, I_NAME, I_DATA FROM ITEM WHERE I_ID = ANY(%s)", [i_ids])
    item_rows = self.cursor.fetchall()
    item_map = {row[0]: (row[1], row[2], row[3]) for row in item_rows}
    return item_map
'''
    violations = lookup_key_not_selected_sql(source)
    assert len(violations) == 1
    assert violations[0]["column"] == "i_id"
    assert violations[0]["function"] == "doNewOrder"


def test_lookup_key_selected_not_flagged():
    source = '''
def doNewOrder(self, i_ids):
    self.cursor.execute("SELECT I_ID, I_PRICE, I_NAME FROM ITEM WHERE I_ID = ANY(%s)", [i_ids])
    item_rows = self.cursor.fetchall()
    item_map = {row[0]: (row[1], row[2]) for row in item_rows}
    return item_map
'''
    assert lookup_key_not_selected_sql(source) == []


def test_lookup_key_composite_filter_skipped():
    source = '''
def get_stock(self, keys):
    self.cursor.execute("SELECT S_QUANTITY, S_DATA FROM STOCK WHERE (S_I_ID, S_W_ID) IN (%s)", [keys])
    stock_rows = self.cursor.fetchall()
    stock_map = {row[0]: row[1] for row in stock_rows}
    return stock_map
'''
    assert lookup_key_not_selected_sql(source) == []


def test_lookup_key_select_star_skipped():
    source = '''
def doNewOrder(self, i_ids):
    self.cursor.execute("SELECT * FROM ITEM WHERE I_ID = ANY(%s)", [i_ids])
    item_rows = self.cursor.fetchall()
    item_map = {row[0]: (row[1], row[2]) for row in item_rows}
    return item_map
'''
    assert lookup_key_not_selected_sql(source) == []


# The shipped doDelivery defect: 1 + 3N placeholders vs 4N parameters.
DELIVERY_REPRO = '''
TXN_QUERIES = {
    "DELIVERY": {
        "updateOrders": "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s AND O_D_ID = %s AND O_W_ID = %s",
        "updateOrderLine": "UPDATE ORDER_LINE SET OL_DELIVERY_D = %s WHERE OL_O_ID = %s AND OL_D_ID = %s AND OL_W_ID = %s",
    }
}


class Reproduced:
    def doDelivery(self, params):
        active_d_ids = params["d_ids"]
        no_o_ids = params["no_o_ids"]
        w_id = params["w_id"]
        o_carrier_id = params["o_carrier_id"]
        ol_delivery_d = params["ol_delivery_d"]

        placeholders_orders = ",".join(["(%s, %s, %s)"] * len(active_d_ids))

        update_orders_params = []
        for no_o_id, d_id in zip(no_o_ids, active_d_ids):
            update_orders_params.extend([o_carrier_id, no_o_id, d_id, w_id])
        self.cursor.execute(
            f"UPDATE ORDERS SET O_CARRIER_ID = %s WHERE (O_ID, O_D_ID, O_W_ID) IN ({placeholders_orders})",
            update_orders_params,
        )

        update_ol_params = []
        for no_o_id, d_id in zip(no_o_ids, active_d_ids):
            update_ol_params.extend([ol_delivery_d, no_o_id, d_id, w_id])
        self.cursor.execute(
            f"UPDATE ORDER_LINE SET OL_DELIVERY_D = %s WHERE (OL_O_ID, OL_D_ID, OL_W_ID) IN ({placeholders_orders})",
            update_ol_params,
        )
        return []
'''


def test_dynamic_placeholder_arity_flags_shipped_delivery_defect():
    violations = dynamic_placeholder_arity_sql(DELIVERY_REPRO)
    assert len(violations) == 2
    assert violations[0]["function"] == "Reproduced.doDelivery"
    assert "UPDATE ORDERS" in violations[0]["sql_shape"]
    assert "UPDATE ORDER_LINE" in violations[1]["sql_shape"]
    assert violations[0]["placeholders"] == "1 + 3*len(active_d_ids)"
    assert violations[0]["params"] == "4*len(active_d_ids)"


def test_dynamic_placeholder_arity_missed_by_static_checks():
    # The two pre-existing gates cannot see the defect at all.
    assert placeholder_param_mismatch_sql(DELIVERY_REPRO) == []
    assert dynamic_placeholder_arity_sql(DELIVERY_REPRO) != []


def test_dynamic_placeholder_arity_hoisted_scalar_not_flagged():
    # Correct form: the scalar is passed once, the per-row list is 3 wide.
    source = '''
def doDelivery(self, params):
    d_ids = params["d_ids"]
    no_o_ids = params["no_o_ids"]
    w_id = params["w_id"]
    o_carrier_id = params["o_carrier_id"]
    placeholders = ",".join(["(%s, %s, %s)"] * len(d_ids))
    update_orders_params = [o_carrier_id]
    for no_o_id, d_id in zip(no_o_ids, d_ids):
        update_orders_params.extend([no_o_id, d_id, w_id])
    self.cursor.execute(
        f"UPDATE ORDERS SET O_CARRIER_ID = %s WHERE (O_ID, O_D_ID, O_W_ID) IN ({placeholders})",
        update_orders_params,
    )
'''
    assert dynamic_placeholder_arity_sql(source) == []


def test_dynamic_placeholder_arity_single_element_not_flagged():
    # n=1 hides the bug, so the unbatched per-district version must stay clean.
    source = '''
def doDelivery(self, params):
    for no_o_id, d_id in zip(params["no_o_ids"], params["d_ids"]):
        self.cursor.execute(
            "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s AND O_D_ID = %s AND O_W_ID = %s",
            [params["o_carrier_id"], no_o_id, d_id, params["w_id"]],
        )
'''
    assert dynamic_placeholder_arity_sql(source) == []


def test_dynamic_placeholder_arity_correct_insert_values_not_flagged():
    source = '''
def insert_rows(self, keys):
    groups = ",".join(["(%s, %s)"] * len(keys))
    params = []
    for key in keys:
        params.extend([key[0], key[1]])
    self.cursor.execute(f"INSERT INTO t (a, b) VALUES {groups}", params)
'''
    assert dynamic_placeholder_arity_sql(source) == []


def test_dynamic_placeholder_arity_execute_batch_not_flagged():
    source = '''
from psycopg2.extras import execute_batch


def update_rows(self, keys, carrier_id):
    groups = ",".join(["(%s, %s, %s)"] * len(keys))
    params = []
    for key in keys:
        params.extend([carrier_id, key[0], key[1], key[2]])
    execute_batch(self.cursor, f"UPDATE ORDERS SET O_CARRIER_ID = %s WHERE (A, B, C) IN ({groups})", params)
    self.cursor.executemany(f"UPDATE ORDERS SET O_CARRIER_ID = %s WHERE (A, B, C) IN ({groups})", params)
'''
    assert dynamic_placeholder_arity_sql(source) == []


def test_dynamic_placeholder_arity_comprehension_ok_not_flagged():
    source = '''
def insert_rows(self, keys):
    groups = ",".join(["(%s, %s)"] * len(keys))
    params = [(a, b) for a, b in keys]
    self.cursor.execute(f"INSERT INTO t (a, b) VALUES {groups}", params)
'''
    assert dynamic_placeholder_arity_sql(source) == []


def test_dynamic_placeholder_arity_comprehension_broken_flagged():
    source = '''
def insert_rows(self, keys):
    groups = ",".join(["(%s, %s)"] * len(keys))
    params = [(a, b, c) for a, b, c in keys]
    self.cursor.execute(f"INSERT INTO t (a, b) VALUES {groups}", params)
'''
    violations = dynamic_placeholder_arity_sql(source)
    assert len(violations) == 1
    assert violations[0]["placeholders"] == "2*len(keys)"
    assert violations[0]["params"] == "3*len(keys)"


def test_dynamic_placeholder_arity_unresolvable_not_flagged():
    source = '''
def update_rows(self, rows):
    groups = ",".join(["(%s, %s)"] * len(rows))
    self.cursor.execute(f"UPDATE ORDERS SET A = %s WHERE (B, C) IN ({groups})", build_params(rows))
    self.cursor.execute(f"UPDATE ORDERS SET A = %s WHERE (B, C) IN ({make_groups(rows)})", rows)
    self.cursor.execute(f"UPDATE ORDERS SET A = %s WHERE (B, C) IN ({groups})", [*rows])
'''
    assert dynamic_placeholder_arity_sql(source) == []


def test_dynamic_placeholder_arity_mismatched_symbols_skipped():
    source = '''
def update_rows(self, keys, other_keys):
    groups = ",".join(["(%s, %s)"] * len(keys))
    params = []
    for key in other_keys:
        params.extend([key[0], key[1]])
    self.cursor.execute(f"INSERT INTO t (a, b) VALUES {groups}", params)
'''
    assert dynamic_placeholder_arity_sql(source) == []


def test_dynamic_placeholder_arity_conditional_mutation_skipped():
    source = '''
def update_rows(self, keys, flag):
    groups = ",".join(["(%s, %s)"] * len(keys))
    params = []
    for key in keys:
        if flag:
            params.extend([key[0], key[1]])
    self.cursor.execute(f"INSERT INTO t (a, b) VALUES {groups}", params)
'''
    assert dynamic_placeholder_arity_sql(source) == []


def test_dynamic_placeholder_arity_syntax_error():
    assert dynamic_placeholder_arity_sql("def f(:") == []


# ponytail: artifact smoke test only; skipped when out/ is absent (e.g. CI).
@pytest.mark.parametrize(
    "name", ["tpcc_run1", "tpcc_run2", "tpcc_run3", "tpcc_rv", "tpcc_rv2", "tpcc_p1"]
)
def test_dynamic_placeholder_arity_real_artifacts_clean(name):
    path = Path(__file__).resolve().parents[1] / "out" / name / "drivers" / "postgresdriver.py"
    if not path.exists():
        pytest.skip(f"{path} not present")
    assert dynamic_placeholder_arity_sql(path.read_text()) == []

