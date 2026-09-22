from src.code_rewriter.tools.sql_analysis import (
    find_select_column_count,
    planner_unfriendly_sql,
    row_index_violations,
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
