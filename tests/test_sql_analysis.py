from pathlib import Path

import pytest

from src.code_rewriter.tools.sql_analysis import (
    composite_any_array_sql,
    ambiguous_values_alias_sql,
    batched_write_tuple_order_sql,
    composite_tuple_order_sql,
    empty_params_with_placeholders_sql,
    write_tables_sql,
    wrong_table_column_sql,
    insert_column_values_arity_sql,
    returned_select_modifiers_sql,
    mogrify_with_params_sql,
    returned_row_widths_sql,
    statement_placeholder_sql,
    table_column_prefix_mismatch_sql,
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
    values_alias_arity_sql,
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



# The shipped DELIVERY regression: 3 placeholders per row, 4 alias columns.
DELIVERY_VALUES_REPRO = '''
def doDelivery(self, params):
    customer_keys = params["customer_keys"]
    flat_cust_params = []
    for c_id, d_id in customer_keys:
        flat_cust_params.append(1.0)
    cust_placeholders = ",".join(["(%s, %s, %s)"] * len(customer_keys))
    for ck in customer_keys:
        flat_cust_params.extend(ck)
    update_cust_sql = (
        "UPDATE CUSTOMER AS c SET C_BALANCE = C_BALANCE + v.ol_total "
        "FROM (VALUES " + cust_placeholders + ") AS v(c_id, c_d_id, c_w_id, ol_total) "
        "WHERE c.C_ID = v.c_id"
    )
    self.cursor.execute(update_cust_sql, flat_cust_params)
'''


def test_values_alias_arity_flags_delivery_customer_update():
    violations = values_alias_arity_sql(DELIVERY_VALUES_REPRO)
    assert len(violations) == 1
    assert violations[0]["function"] == "doDelivery"
    assert violations[0]["row_width"] == 3
    assert violations[0]["alias_columns"] == 4


def test_values_alias_arity_accepts_matching_row_width():
    source = '''
def doDelivery(self, params):
    keys = params["keys"]
    placeholders = ",".join(["(%s, %s, %s)"] * len(keys))
    update_sql = f"UPDATE t AS x SET v = s.v FROM (VALUES {placeholders}) AS s(a, b, c, v)"
    self.cursor.execute(update_sql, [])
'''
    # Alias has 4 columns but the row carries 3 -> still a mismatch; use a
    # literal 4-wide pattern for the positive control.
    assert len(values_alias_arity_sql(source)) == 1
    source = source.replace('["(%s, %s, %s)"]', '["(%s, %s, %s, %s)"]')
    assert values_alias_arity_sql(source) == []


def test_values_alias_arity_ignores_in_clause():
    source = '''
def doDelivery(self, params):
    keys = params["keys"]
    placeholders = ",".join(["(%s, %s, %s)"] * len(keys))
    self.cursor.execute(
        f"UPDATE t SET c = %s WHERE (a, b, c) IN ({placeholders})", []
    )
'''
    assert values_alias_arity_sql(source) == []


# ponytail: artifact smoke test only; skipped when out/ is absent (e.g. CI).
def test_values_alias_arity_real_delivery_defect():
    path = (
        Path(__file__).resolve().parents[1]
        / "out"
        / "tpcc_p0p1"
        / "drivers"
        / "postgresdriver.py"
    )
    if not path.exists():
        pytest.skip(f"{path} not present")
    violations = values_alias_arity_sql(path.read_text())
    assert len(violations) == 1
    assert violations[0]["function"] == "PostgresDriver.doDelivery"
    assert (violations[0]["row_width"], violations[0]["alias_columns"]) == (3, 4)


# The second shipped DELIVERY regression: valid_orders is (d_id, no_o_id) but
# the batched predicates are (O_ID, O_D_ID) / (OL_O_ID, OL_D_ID).
DELIVERY_TUPLE_ORDER_REPRO = '''
def doDelivery(self, params):
    w_id = params["w_id"]
    d_ids = list(range(1, 11))

    self.cursor.execute(
        "SELECT NO_D_ID, MIN(NO_O_ID) FROM NEW_ORDER WHERE NO_D_ID = ANY(%s) AND NO_W_ID = %s GROUP BY NO_D_ID",
        [d_ids, w_id],
    )
    new_order_map = {row[0]: row[1] for row in self.cursor.fetchall()}

    valid_orders = []
    for d_id in d_ids:
        no_o_id = new_order_map.get(d_id)
        if no_o_id is None:
            continue
        valid_orders.append((d_id, no_o_id))

    self.cursor.execute(
        "SELECT O_ID, O_D_ID, O_C_ID FROM ORDERS WHERE O_W_ID = %s AND (O_ID, O_D_ID) IN (%s)"
        % ("%s", ",".join(["(%s,%s)" for _ in valid_orders])),
        [w_id] + [val for pair in valid_orders for val in pair],
    )
    order_map = {(row[0], row[1]): row[2] for row in self.cursor.fetchall()}
    for d_id, no_o_id in valid_orders:
        assert order_map.get((no_o_id, d_id)) is not None
'''


def test_composite_tuple_order_flags_swapped_delivery_key():
    violations = composite_tuple_order_sql(DELIVERY_TUPLE_ORDER_REPRO)
    assert len(violations) == 1
    assert violations[0]["function"] == "doDelivery"
    assert violations[0]["columns"] == ["o_id", "o_d_id"]
    assert violations[0]["tuple_order"] == ["d_id", "no_o_id"]
    assert violations[0]["expected_order"] == ["o_d_id", "o_id"]


def test_composite_tuple_order_accepts_correct_order():
    source = DELIVERY_TUPLE_ORDER_REPRO.replace(
        'valid_orders.append((d_id, no_o_id))',
        'valid_orders.append((no_o_id, d_id))',
    )
    assert composite_tuple_order_sql(source) == []


def test_composite_tuple_order_ignores_unresolvable_names():
    source = '''
def f(self, rows):
    self.cursor.execute(
        "SELECT O_ID FROM ORDERS WHERE (O_ID, O_D_ID) IN (%s)",
        [val for pair in rows for val in pair],
    )
'''
    assert composite_tuple_order_sql(source) == []


def test_table_column_prefix_mismatch_flags_truncated_order_line_column():
    source = '''
def doDelivery(self, params):
    self.cursor.execute(
        "UPDATE ORDER_LINE SET OL_DELIVERY_D = %s WHERE O_W_ID = %s AND (OL_O_ID, OL_D_ID) IN (%s)",
        [],
    )
'''
    violations = table_column_prefix_mismatch_sql(source)
    assert len(violations) == 1
    assert violations[0]["function"] == "doDelivery"
    assert violations[0]["column"] == "O_W_ID"
    assert violations[0]["expected_prefix"] == "OL"


def test_table_column_prefix_mismatch_accepts_full_prefix():
    source = '''
def doDelivery(self, params):
    self.cursor.execute(
        "UPDATE ORDER_LINE SET OL_DELIVERY_D = %s WHERE OL_W_ID = %s AND (OL_O_ID, OL_D_ID) IN (%s)",
        [],
    )
'''
    assert table_column_prefix_mismatch_sql(source) == []


def test_table_column_prefix_mismatch_skips_joins():
    source = '''
def doDelivery(self, params):
    self.cursor.execute(
        "SELECT O_ID FROM ORDERS JOIN ORDER_LINE ON O_ID = OL_O_ID WHERE O_W_ID = %s",
        [],
    )
'''
    assert table_column_prefix_mismatch_sql(source) == []


ORDER_STATUS_ROWS = '''
TXN_QUERIES = {"ORDER_STATUS": {
  "getCustomerByCustomerId": "SELECT C_ID, C_FIRST, C_MIDDLE, C_LAST, C_BALANCE FROM CUSTOMER WHERE C_W_ID = %s",
  "getCustomersByLastName": "SELECT C_ID, C_FIRST, C_MIDDLE, C_LAST, C_BALANCE FROM CUSTOMER WHERE C_W_ID = %s",
  "getLastOrder": "SELECT O_ID, O_CARRIER_ID, O_ENTRY_D FROM ORDERS WHERE O_W_ID = %s",
  "getOrderLines": "SELECT OL_SUPPLY_W_ID, OL_I_ID, OL_QUANTITY, OL_AMOUNT, OL_DELIVERY_D FROM ORDER_LINE WHERE OL_W_ID = %s",
}}

class D:
    def doOrderStatus(self, params):
        q = TXN_QUERIES["ORDER_STATUS"]
        c_id = params["c_id"]
        if c_id is not None:
            self.cursor.execute(q["getCustomerByCustomerId"], [1])
            customer = self.cursor.fetchone()
        else:
            self.cursor.execute(q["getCustomersByLastName"], [1])
            all_customers = self.cursor.fetchall()
            customer = all_customers[0]
        self.cursor.execute(q["getLastOrder"], [1])
        order = self.cursor.fetchone()
        if order:
            self.cursor.execute(q["getOrderLines"], [1])
            orderLines = self.cursor.fetchall()
        else:
            orderLines = []
        return [customer, order, orderLines]
'''

ORDER_STATUS_WIDENED = ORDER_STATUS_ROWS.replace(
    'q["getCustomerByCustomerId"], [1]',
    '"SELECT C_ID, C_FIRST, C_MIDDLE, C_LAST, C_BALANCE, C_STREET_1, C_CITY, C_BALANCE, C_DATA FROM CUSTOMER", [1]',
).replace(
    'q["getCustomersByLastName"], [1]',
    '"SELECT C_ID, C_FIRST, C_MIDDLE, C_LAST, C_BALANCE, C_STREET_1, C_CITY, C_BALANCE, C_DATA FROM CUSTOMER", [1]',
)


def test_returned_row_widths_tracks_fetched_rows():
    widths = returned_row_widths_sql(ORDER_STATUS_ROWS)["D.doOrderStatus"]
    assert widths == {"customer": [5], "order": [3], "orderLines": [5]}


def test_returned_row_widths_detects_widened_projection():
    widths = returned_row_widths_sql(ORDER_STATUS_WIDENED)["D.doOrderStatus"]
    assert widths["customer"] == [9]
    assert widths["order"] == [3]
    assert widths["orderLines"] == [5]


def test_returned_row_widths_respects_slices():
    source = '''
class D:
    def doPayment(self, params):
        self.cursor.execute(
            "SELECT W_NAME, W_STREET_1, W_STREET_2, W_CITY, W_STATE, W_ZIP, "
            "D_NAME, D_STREET_1, D_STREET_2, D_CITY, D_STATE, D_ZIP "
            "FROM WAREHOUSE, DISTRICT",
            [],
        )
        res = self.cursor.fetchone()
        warehouse = res[0:6]
        district = res[6:12]
        return [warehouse, district]
'''
    widths = returned_row_widths_sql(source)["D.doPayment"]
    assert widths == {"warehouse": [6], "district": [6]}


def test_ambiguous_values_alias_flags_unqualified_target_column():
    source = '''
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
'''
    violations = ambiguous_values_alias_sql(source)
    assert len(violations) == 1
    assert violations[0]["function"] == "doDelivery"
    assert violations[0]["columns"] == ["o_id"]


def test_ambiguous_values_alias_accepts_qualified_target_columns():
    source = '''
def doDelivery(self, params):
    keys = params["keys"]
    groups = ",".join(["(%s, %s, %s, %s)"] * len(keys))
    flat = [val for tup in keys for val in tup]
    self.cursor.execute(
        f"UPDATE ORDERS SET O_CARRIER_ID = v.carrier_id "
        f"FROM (VALUES {groups}) AS v(carrier_id, o_id, d_id, w_id) "
        f"WHERE ORDERS.O_ID = v.o_id AND ORDERS.O_D_ID = v.d_id AND ORDERS.O_W_ID = v.w_id",
        flat,
    )
'''
    assert ambiguous_values_alias_sql(source) == []


def test_ambiguous_values_alias_ignores_non_colliding_alias():
    source = '''
def doDelivery(self, params):
    keys = params["keys"]
    groups = ",".join(["(%s, %s, %s, %s)"] * len(keys))
    flat = [val for tup in keys for val in tup]
    self.cursor.execute(
        f"UPDATE ORDER_LINE SET OL_DELIVERY_D = v.del_d "
        f"FROM (VALUES {groups}) AS v(del_d, o_id, d_id, w_id) "
        f"WHERE OL_O_ID = v.o_id AND OL_D_ID = v.d_id AND OL_W_ID = v.w_id",
        flat,
    )
'''
    assert ambiguous_values_alias_sql(source) == []


def test_statement_placeholder_flags_cte_body():
    source = '''
def doPayment(self, params):
    self.cursor.execute(
        "WITH u1 AS (UPDATE WAREHOUSE SET W_YTD = W_YTD + %s WHERE W_ID = %s RETURNING 1), "
        "u2 AS (UPDATE DISTRICT SET D_YTD = D_YTD + %s WHERE D_W_ID = %s AND D_ID = %s RETURNING 1), "
        "u3 AS (%s RETURNING 1) INSERT INTO HISTORY VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        params,
    )
'''
    violations = statement_placeholder_sql(source)
    assert len(violations) == 1
    assert violations[0]["function"] == "doPayment"
    assert "AS (%s RETURNING" in violations[0]["fragment"]


def test_statement_placeholder_accepts_inlined_cte():
    source = '''
def doPayment(self, params):
    self.cursor.execute(
        "WITH u1 AS (UPDATE WAREHOUSE SET W_YTD = W_YTD + %s WHERE W_ID = %s RETURNING 1), "
        "u2 AS (UPDATE DISTRICT SET D_YTD = D_YTD + %s WHERE D_W_ID = %s AND D_ID = %s RETURNING 1), "
        "u3 AS (UPDATE CUSTOMER SET C_BALANCE = %s WHERE C_ID = %s RETURNING 1) "
        "INSERT INTO HISTORY VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        params,
    )
'''
    assert statement_placeholder_sql(source) == []


def test_statement_placeholder_ignores_values_list():
    source = '''
def doPayment(self, params):
    self.cursor.execute(
        "INSERT INTO HISTORY VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        params,
    )
'''
    assert statement_placeholder_sql(source) == []


def test_mogrify_with_params_flags_duplicate_values():
    source = '''
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
'''
    violations = mogrify_with_params_sql(source)
    assert len(violations) == 1
    assert violations[0]["function"] == "doDelivery"


def test_mogrify_with_params_flags_remaining_placeholder_plus_flattening():
    source = '''
def doDelivery(self, params):
    cust_params = params["custs"]
    args_str_cust = ",".join(
        self.cursor.mogrify("(%s, %s, %s)", p).decode("utf-8")
        for p in cust_params
    )
    flat_cust_params = [val for p in cust_params for val in p]
    self.cursor.execute(
        f"UPDATE CUSTOMER AS c SET C_BALANCE = C_BALANCE + v.amt "
        f"FROM (VALUES {args_str_cust}) AS v(amt, c_id, c_w_id) "
        f"WHERE c.C_ID = v.c_id AND c.C_D_ID = %s AND c.C_W_ID = v.c_w_id",
        flat_cust_params + [params["d_id"]],
    )
'''
    assert len(mogrify_with_params_sql(source)) == 1


def test_mogrify_with_params_accepts_params_for_other_placeholders():
    source = '''
def doDelivery(self, params):
    rows = params["rows"]
    args_str = ",".join(
        self.cursor.mogrify("(%s, %s, %s)", p).decode("utf-8")
        for p in rows
    )
    self.cursor.execute(
        f"DELETE FROM t WHERE (a, b, c) IN ({args_str}) AND w = %s",
        [params["w"]],
    )
'''
    assert mogrify_with_params_sql(source) == []


def test_empty_params_with_placeholders_flags_dead_update():
    source = '''
def doDelivery(self, params):
    order_tuples = params["orders"]
    placeholders = ",".join(["(%s, %s, %s)"] * len(order_tuples))
    self.cursor.execute(
        f"UPDATE ORDERS SET O_CARRIER_ID = %s WHERE (O_ID, O_D_ID, O_W_ID) IN ({placeholders})",
        [params["carrier"]] * len(order_tuples) if False else [],
    )
'''
    violations = empty_params_with_placeholders_sql(source)
    assert len(violations) == 1
    assert violations[0]["function"] == "doDelivery"


def test_empty_params_with_placeholders_accepts_placeholder_free_sql():
    source = '''
def ping(self):
    self.cursor.execute("SELECT 1", [])
'''
    assert empty_params_with_placeholders_sql(source) == []


def test_empty_params_with_placeholders_accepts_real_params():
    source = '''
def update(self, a, b):
    self.cursor.execute("UPDATE t SET x = %s WHERE y = %s", [a, b])
'''
    assert empty_params_with_placeholders_sql(source) == []


def test_table_column_prefix_mismatch_sees_dynamic_join_fragment():
    source = '''
def doDelivery(self, params):
    order_keys = params["keys"]
    placeholders = " OR ".join(["(O_D_ID = %s AND O_ID = %s)"] * len(order_keys))
    self.cursor.execute(
        f"SELECT SUM(OL_AMOUNT) FROM ORDER_LINE WHERE OL_W_ID = %s AND ({placeholders})",
        params["params"],
    )
'''
    violations = table_column_prefix_mismatch_sql(source)
    assert {v["column"] for v in violations} == {"O_D_ID", "O_ID"}
    assert all(v["expected_prefix"] == "OL" for v in violations)


def test_table_column_prefix_mismatch_dynamic_fragment_accepts_orders():
    source = '''
def doDelivery(self, params):
    order_keys = params["keys"]
    placeholders = " OR ".join(["(O_D_ID = %s AND O_ID = %s)"] * len(order_keys))
    self.cursor.execute(
        f"SELECT O_D_ID, O_ID, O_C_ID FROM ORDERS WHERE O_W_ID = %s AND ({placeholders})",
        params["params"],
    )
'''
    assert table_column_prefix_mismatch_sql(source) == []


def test_composite_tuple_order_flags_loop_built_param_list():
    source = '''
def doDelivery(self, params):
    order_tuples = [(no_o_id, d_id, w_id) for d_id, no_o_id in params["orders"]]
    flat = []
    for t in order_tuples:
        flat.extend(t)
    self.cursor.execute(
        "DELETE FROM NEW_ORDER WHERE (NO_D_ID, NO_W_ID, NO_O_ID) IN (%s)",
        flat,
    )
'''
    violations = composite_tuple_order_sql(source)
    assert len(violations) == 1
    assert violations[0]["tuple_order"] == ["no_o_id", "d_id", "w_id"]
    assert violations[0]["expected_order"] == ["no_o_id", "no_d_id", "no_w_id"]


def test_composite_tuple_order_accepts_loop_built_correct_delete():
    source = '''
def doDelivery(self, params):
    delete_tuples = [(d_id, w_id, no_o_id) for d_id, no_o_id in params["orders"]]
    flat = []
    for t in delete_tuples:
        flat.extend(t)
    self.cursor.execute(
        "DELETE FROM NEW_ORDER WHERE (NO_D_ID, NO_W_ID, NO_O_ID) IN (%s)",
        flat,
    )
'''
    assert composite_tuple_order_sql(source) == []


BATCHED_DELIVERY_WRITES = '''
TXN_QUERIES = {"DELIVERY": {
    "deleteNewOrder": "DELETE FROM NEW_ORDER WHERE NO_D_ID = %s AND NO_W_ID = %s AND NO_O_ID = %s",
    "updateOrders": "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s AND O_D_ID = %s AND O_W_ID = %s",
}}
class D:
    def doDelivery(self, params):
        q = TXN_QUERIES["DELIVERY"]
        __BODY__
        self.cursor.execute_batch(self.cursor, q["deleteNewOrder"], deletes)
        self.cursor.execute_batch(self.cursor, q["updateOrders"], updates)
'''


def test_batched_write_tuple_order_flags_reused_orders_key_order():
    source = BATCHED_DELIVERY_WRITES.replace(
        "__BODY__",
        """
        order_tuples = [(no_o_id, d_id, w_id) for d_id, no_o_id in params["orders"]]
        deletes = order_tuples
        updates = order_tuples
""",
    )
    violations = batched_write_tuple_order_sql(source)
    assert len(violations) == 1
    assert violations[0]["columns"] == ["no_d_id", "no_w_id", "no_o_id"]
    assert violations[0]["tuple_order"] == ["no_o_id", "d_id", "w_id"]
    assert violations[0]["expected_order"] == ["no_o_id", "no_d_id", "no_w_id"]


def test_batched_write_tuple_order_accepts_each_table_key_order():
    source = BATCHED_DELIVERY_WRITES.replace(
        "__BODY__",
        """
        deletes = [(d_id, w_id, no_o_id) for d_id, no_o_id in params["orders"]]
        updates = [(params["carrier"], no_o_id, d_id, w_id) for d_id, no_o_id in params["orders"]]
""",
    )
    assert batched_write_tuple_order_sql(source) == []


def test_write_tables_includes_execute_batch():
    source = '''
TXN_QUERIES = {"DELIVERY": {
    "deleteNewOrder": "DELETE FROM NEW_ORDER WHERE NO_D_ID = %s AND NO_W_ID = %s AND NO_O_ID = %s",
    "updateOrders": "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s AND O_D_ID = %s AND O_W_ID = %s",
}}
class D:
    def doDelivery(self, params):
        q = TXN_QUERIES["DELIVERY"]
        from psycopg2.extras import execute_batch
        execute_batch(self.cursor, q["deleteNewOrder"], params["deletes"])
        execute_batch(self.cursor, q["updateOrders"], params["updates"])
'''
    assert write_tables_sql(source)["D.doDelivery"] == {
        ("DELETE", "NEW_ORDER"), ("UPDATE", "ORDERS"),
    }


def test_write_tables_drops_are_visible_as_set_difference():
    source = '''
class D:
    def doDelivery(self, params):
        self.cursor.execute(
            "UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s",
            [params["c"], params["o"]],
        )
'''
    assert write_tables_sql(source)["D.doDelivery"] == {("UPDATE", "ORDERS")}


def test_returned_modifiers_tracks_order_by_and_limit():
    source = '''
class D:
    def doOrderStatus(self, params):
        self.cursor.execute(
            "SELECT O_ID, O_CARRIER_ID, O_ENTRY_D FROM ORDERS WHERE O_W_ID = %s ORDER BY O_ID DESC LIMIT 1",
            [params["w"]],
        )
        order = self.cursor.fetchone()
        self.cursor.execute("SELECT C_ID FROM CUSTOMER WHERE C_W_ID = %s", [params["w"]])
        customer = self.cursor.fetchone()
        return [customer, order]
'''
    mods = returned_select_modifiers_sql(source)["D.doOrderStatus"]
    assert mods["order"] == {"order_by": [True], "limit": [True], "join": [False]}
    assert mods["customer"] == {"order_by": [False], "limit": [False], "join": [False]}


def test_wrong_table_column_flags_orders_key_in_order_line():
    orig = '''
class D:
    def doDelivery(self, params):
        self.cursor.execute(
            "SELECT O_C_ID FROM ORDERS WHERE O_ID = %s AND O_D_ID = %s AND O_W_ID = %s",
            params["p"],
        )
        self.cursor.execute(
            "SELECT SUM(OL_AMOUNT) FROM ORDER_LINE WHERE OL_W_ID = %s AND OL_D_ID = %s",
            params["p"],
        )
'''
    bad = '''
def doDelivery(self, params):
    order_keys = params["keys"]
    placeholders = " OR ".join(["(O_D_ID = %s AND O_ID = %s)"] * len(order_keys))
    self.cursor.execute(
        f"SELECT SUM(OL_AMOUNT) FROM ORDER_LINE WHERE OL_W_ID = %s AND ({placeholders})",
        params["p"],
    )
'''
    violations = wrong_table_column_sql(orig, bad)
    assert {v["column"] for v in violations} == {"O_D_ID", "O_ID"}
    assert all(v["table"] == "ORDER_LINE" and v["belongs_to"] == ["ORDERS"] for v in violations)


def test_wrong_table_column_accepts_own_columns():
    orig = '''
class D:
    def f(self, params):
        self.cursor.execute("SELECT O_C_ID FROM ORDERS WHERE O_ID = %s", params["p"])
'''
    good = '''
def f(self, params):
    self.cursor.execute("SELECT O_ID, O_C_ID FROM ORDERS WHERE O_W_ID = %s", params["p"])
'''
    assert wrong_table_column_sql(orig, good) == []


def test_insert_column_values_arity_flags_short_row():
    source = '''
def doNewOrder(self, params):
    self.cursor.execute(
        "INSERT INTO ORDER_LINE (OL_O_ID, OL_D_ID, OL_W_ID) VALUES (%s, %s)",
        params["p"],
    )
'''
    violations = insert_column_values_arity_sql(source)
    assert len(violations) == 1
    assert (violations[0]["columns"], violations[0]["values"]) == (3, 2)


def test_insert_column_values_arity_accepts_matching_row():
    source = '''
def doNewOrder(self, params):
    self.cursor.execute(
        "INSERT INTO ORDER_LINE (OL_O_ID, OL_D_ID, OL_W_ID) VALUES (%s, %s, %s)",
        params["p"],
    )
'''
    assert insert_column_values_arity_sql(source) == []


def test_returned_modifiers_traces_comprehension_to_join():
    source = '''
class D:
    def doOrderStatus(self, params):
        self.cursor.execute(
            "SELECT O_ID FROM ORDERS WHERE O_W_ID = %s ORDER BY O_ID DESC LIMIT 1",
            params["p"],
        )
        rows = self.cursor.fetchall()
        order = (rows[0][0], rows[0][1])
        orderLines = [(r[2], r[3]) for r in rows]
        return [order, orderLines]
'''
    mods = returned_select_modifiers_sql(source)["D.doOrderStatus"]
    assert mods["orderLines"]["join"] == [False]
    assert mods["orderLines"]["limit"] == [True]
    assert mods["order"]["limit"] == [True]


def test_return_modifiers_join_flag_on_explicit_join():
    source = '''
class D:
    def f(self, params):
        self.cursor.execute(
            "SELECT O.O_ID, OL.OL_I_ID FROM ORDERS O "
            "JOIN ORDER_LINE OL ON OL.OL_O_ID = O.O_ID "
            "WHERE O.O_W_ID = %s ORDER BY O.O_ID DESC LIMIT 1",
            params["p"],
        )
        rows = self.cursor.fetchall()
        return [rows]
'''
    mods = returned_select_modifiers_sql(source)["D.f"]
    assert mods["rows"] == {"order_by": [True], "limit": [True], "join": [True]}
