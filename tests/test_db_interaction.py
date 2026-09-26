import ast
from pathlib import Path

from src.code_rewriter.tools.ast_analyzer import analyze_file
from src.code_rewriter.tools.db_interaction import (
    analyze_sql,
    build_function_model,
    build_read_write_map,
    normalize_placeholders,
)
from src.code_rewriter.tools.sql_resolver import collect_module_dicts

TPCC_DRIVER = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "tools"
    / "tpcc"
    / "drivers"
    / "postgresdriver.py"
)


def _driver_source() -> str:
    return TPCC_DRIVER.read_text(encoding="utf-8")


def _function_node(tree: ast.Module, class_name: str, func_name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == func_name:
                    return child
    raise AssertionError(f"{class_name}.{func_name} not found")


def _driver_operation_sql(fragment: str) -> str:
    analysis = analyze_file(TPCC_DRIVER)
    for op in analysis.database_operations:
        if op.sql and fragment in op.sql:
            return op.sql
    raise AssertionError(f"no resolved SQL containing {fragment!r}")


# ---------------------------------------------------------------------------
# normalize_placeholders
# ---------------------------------------------------------------------------

def test_normalize_percent_s():
    assert normalize_placeholders("SELECT * FROM t WHERE a = %s AND b = %s") == (
        "SELECT * FROM t WHERE a = ? AND b = ?"
    )


def test_normalize_question_mark_unchanged():
    assert normalize_placeholders("SELECT * FROM t WHERE a = ?") == (
        "SELECT * FROM t WHERE a = ?"
    )


def test_normalize_dollar_and_named():
    assert normalize_placeholders("SELECT * FROM t WHERE a = $1 AND b = :name") == (
        "SELECT * FROM t WHERE a = ? AND b = ?"
    )


def test_normalize_preserves_string_literals_and_casts():
    sql = "SELECT ':name', '%s' FROM t WHERE a = %s AND b = x::int"
    assert normalize_placeholders(sql) == (
        "SELECT ':name', '%s' FROM t WHERE a = ? AND b = x::int"
    )


# ---------------------------------------------------------------------------
# analyze_sql
# ---------------------------------------------------------------------------

def test_analyze_sql_new_order():
    sql = _driver_operation_sql("NO_O_ID FROM NEW_ORDER")
    model = analyze_sql(sql)
    assert model is not None
    assert set(model.tables_read) == {"NEW_ORDER"}
    assert model.top_level_relations == 1
    assert model.has_aggregate is False


def test_analyze_sql_stock_count_original():
    sql = _driver_operation_sql("COUNT(DISTINCT(OL_I_ID))")
    model = analyze_sql(sql)
    assert model is not None
    assert set(model.tables_read) == {"ORDER_LINE", "STOCK"}
    assert model.top_level_relations == 2
    assert model.has_aggregate is True
    assert model.has_distinct is True


def test_analyze_sql_three_relation_rewrite():
    sql = (
        "SELECT COUNT(DISTINCT OL_I_ID) FROM ORDER_LINE "
        "JOIN STOCK ON ORDER_LINE.OL_I_ID = STOCK.S_I_ID "
        "JOIN DISTRICT ON STOCK.S_W_ID = DISTRICT.D_W_ID "
        "WHERE OL_W_ID = %s"
    )
    model = analyze_sql(sql)
    assert model is not None
    assert model.top_level_relations == 3
    assert model.join_count == 2
    assert "DISTRICT" in model.tables_read


def test_analyze_sql_malformed_returns_none():
    assert analyze_sql("SELECT * FROM") is None
    assert analyze_sql("SELECT (") is None
    assert analyze_sql("") is None


# ---------------------------------------------------------------------------
# build_function_model
# ---------------------------------------------------------------------------

def test_build_function_model_stock_level_value_edge():
    tree = ast.parse(_driver_source())
    module_dicts = collect_module_dicts(tree)
    fn = _function_node(tree, "PostgresDriver", "doStockLevel")

    model = build_function_model(
        fn, module_dicts, file="postgresdriver.py", function="PostgresDriver.doStockLevel"
    )
    assert len(model.statements) == 2
    assert (0, 1) in model.value_edges


def test_build_function_model_delivery_has_no_value_edges():
    tree = ast.parse(_driver_source())
    module_dicts = collect_module_dicts(tree)
    fn = _function_node(tree, "PostgresDriver", "doDelivery")

    model = build_function_model(
        fn, module_dicts, file="postgresdriver.py", function="PostgresDriver.doDelivery"
    )
    assert len(model.statements) > 0
    assert model.value_edges == []


# ---------------------------------------------------------------------------
# build_read_write_map
# ---------------------------------------------------------------------------

def test_build_read_write_map_district():
    analysis = analyze_file(TPCC_DRIVER)
    rw = build_read_write_map({"postgresdriver.py": analysis})

    assert "DISTRICT" in rw
    assert "PostgresDriver.doNewOrder" in rw["DISTRICT"]["written_by"]
    assert "PostgresDriver.doStockLevel" in rw["DISTRICT"]["read_by"]
