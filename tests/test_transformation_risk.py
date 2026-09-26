"""Tests for advisory transformation-risk reporting (Phase 2)."""

import ast
from pathlib import Path

from src.code_rewriter.tools.ast_analyzer import analyze_file
from src.code_rewriter.tools.ast_replacer import replace_function_ast
from src.code_rewriter.tools.db_interaction import build_read_write_map
from src.code_rewriter.tools.transformation_risk import (
    AGGREGATE_QUERY_EXPANSION,
    CROSS_FUNCTION_READ_WRITE,
    DEPENDENT_QUERY_FUSION,
    JOIN_COMPLEXITY_INCREASE,
    analyze_candidate,
    analyze_transformation,
    compare_function_models,
    model_from_source,
)

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


def _function_source(source: str, class_name: str, func_name: str) -> str:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == func_name:
                    segment = ast.get_source_segment(source, child)
                    if segment:
                        return segment
    raise AssertionError(f"{class_name}.{func_name} not found")


# Rewritten doStockLevel: two sequential reads fused into one 3-relation JOIN.
REWRITTEN_STOCK_LEVEL = '''
def doStockLevel(self, params):
    w_id = params["w_id"]
    d_id = params["d_id"]
    threshold = params["threshold"]
    self.cursor.execute(
        "SELECT COUNT(DISTINCT OL_I_ID) FROM ORDER_LINE "
        "JOIN STOCK ON ORDER_LINE.OL_I_ID = STOCK.S_I_ID "
        "JOIN DISTRICT ON STOCK.S_W_ID = DISTRICT.D_W_ID "
        "WHERE OL_W_ID = %s AND OL_D_ID = %s AND OL_O_ID < %s",
        [w_id, d_id, threshold],
    )
    result = self.cursor.fetchone()
    self.conn.commit()
    return int(result[0])
'''


# Batched doDelivery: no linear value dependencies, different op multiset.
BATCHED_DELIVERY = '''
def doDelivery(self, params):
    w_id = params["w_id"]
    self.cursor.execute("SELECT NO_O_ID FROM NEW_ORDER WHERE NO_W_ID = %s", [w_id])
    rows = self.cursor.fetchall()
    self.cursor.execute("DELETE FROM NEW_ORDER WHERE NO_W_ID = %s", [w_id])
    self.cursor.execute("UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_W_ID = %s", [w_id])
    self.cursor.execute("UPDATE ORDER_LINE SET OL_DELIVERY_D = %s WHERE OL_W_ID = %s", [w_id])
    self.cursor.execute("UPDATE CUSTOMER SET C_BALANCE = %s WHERE C_W_ID = %s", [w_id])
    self.conn.commit()
    return []
'''


SCALAR_SUBQUERY_ORIG = '''
def compute(self, w_id):
    self.cursor.execute(
        "SELECT MAX(O_ID) FROM ORDERS JOIN ORDER_LINE ON O_ID = OL_O_ID WHERE O_W_ID = %s",
        [w_id],
    )
    row = self.cursor.fetchone()
    o_id = row[0]
    self.cursor.execute(
        "SELECT COUNT(OL_ID) FROM ORDER_LINE JOIN STOCK ON OL_I_ID = S_I_ID WHERE OL_O_ID = %s",
        [o_id],
    )
    result = self.cursor.fetchone()
    return result[0]
'''


SCALAR_SUBQUERY_OPT = '''
def compute(self, w_id):
    self.cursor.execute(
        "SELECT COUNT(OL_ID) FROM ORDER_LINE JOIN STOCK ON OL_I_ID = S_I_ID "
        "WHERE OL_O_ID = (SELECT MAX(O_ID) FROM ORDERS) AND OL_W_ID = %s",
        [w_id],
    )
    result = self.cursor.fetchone()
    return result[0]
'''


LOCAL_ORIG = '''
def update_row(self, value):
    self.cursor.execute("UPDATE T SET A = %s WHERE ID = %s", [value, 1])
    self.cursor.execute("UPDATE T SET B = %s WHERE ID = %s", [value, 2])
    return 1
'''


LOCAL_OPT = '''
def update_row(self, value):
    self.cursor.execute("UPDATE T SET A = %s, B = %s WHERE ID = %s", [value, value, 1])
    return 1
'''


# ---------------------------------------------------------------------------
# model_from_source
# ---------------------------------------------------------------------------

def test_model_from_source_finds_stock_level():
    model = model_from_source(_driver_source(), "PostgresDriver.doStockLevel")
    assert model is not None
    assert len(model.statements) == 2
    assert (0, 1) in model.value_edges


def test_model_from_source_not_found_returns_none():
    assert model_from_source(_driver_source(), "PostgresDriver.nope") is None
    assert model_from_source("def broken(", "f") is None
    assert model_from_source("", "f") is None


# ---------------------------------------------------------------------------
# compare_function_models
# ---------------------------------------------------------------------------

def test_high_risk_stock_level_join_rewrite():
    orig = model_from_source(_driver_source(), "PostgresDriver.doStockLevel")
    opt = model_from_source(REWRITTEN_STOCK_LEVEL, "doStockLevel")
    assert orig is not None and opt is not None

    read_write_map = build_read_write_map(analyze_file(TPCC_DRIVER))
    report = compare_function_models(
        orig, opt, read_write_map, function="PostgresDriver.doStockLevel"
    )

    assert report.risk == "HIGH"
    assert {
        DEPENDENT_QUERY_FUSION,
        JOIN_COMPLEXITY_INCREASE,
        AGGREGATE_QUERY_EXPANSION,
        CROSS_FUNCTION_READ_WRITE,
    }.issubset(set(report.flags))
    assert report.statements_before == 2
    assert report.statements_after == 1
    assert report.max_relations_before == 2
    assert report.max_relations_after == 3
    assert report.fused_dependencies == 1


def test_low_risk_delivery_batching():
    orig = model_from_source(_driver_source(), "PostgresDriver.doDelivery")
    opt = model_from_source(BATCHED_DELIVERY, "doDelivery")
    assert orig is not None and opt is not None
    assert orig.value_edges == []
    assert opt.value_edges == []

    report = compare_function_models(
        orig, opt, {}, function="PostgresDriver.doDelivery"
    )

    assert report.risk == "LOW"
    assert DEPENDENT_QUERY_FUSION not in report.flags


def test_medium_risk_scalar_subquery_fusion():
    orig = model_from_source(SCALAR_SUBQUERY_ORIG, "compute")
    opt = model_from_source(SCALAR_SUBQUERY_OPT, "compute")
    assert orig is not None and opt is not None
    assert orig.value_edges

    report = compare_function_models(orig, opt, {}, function="compute")

    assert report.risk == "MEDIUM"
    assert DEPENDENT_QUERY_FUSION in report.flags
    assert JOIN_COMPLEXITY_INCREASE not in report.flags
    assert AGGREGATE_QUERY_EXPANSION not in report.flags
    assert CROSS_FUNCTION_READ_WRITE not in report.flags
    assert report.max_relations_before == report.max_relations_after


def test_low_risk_local_simplification():
    orig = model_from_source(LOCAL_ORIG, "update_row")
    opt = model_from_source(LOCAL_OPT, "update_row")
    assert orig is not None and opt is not None

    report = compare_function_models(orig, opt, {}, function="update_row")

    assert report.risk == "LOW"
    assert report.flags == []
    assert report.fused_dependencies == 0


# ---------------------------------------------------------------------------
# analyze_candidate
# ---------------------------------------------------------------------------

def test_analyze_candidate_high_risk_stock_level():
    read_write_map = build_read_write_map(analyze_file(TPCC_DRIVER))
    report = analyze_candidate(
        _driver_source(),
        REWRITTEN_STOCK_LEVEL,
        "PostgresDriver.doStockLevel",
        read_write_map,
    )
    assert report is not None
    assert report.risk == "HIGH"


def test_analyze_candidate_missing_function_returns_none():
    assert analyze_candidate(_driver_source(), REWRITTEN_STOCK_LEVEL, "Nope.nope", {}) is None
    assert analyze_candidate("", REWRITTEN_STOCK_LEVEL, "doStockLevel", {}) is None
    assert analyze_candidate(_driver_source(), "def broken(", "doStockLevel", {}) is None


# ---------------------------------------------------------------------------
# analyze_transformation
# ---------------------------------------------------------------------------

def test_analyze_transformation_missing_files_returns_none(tmp_path):
    contract = {
        "target": {
            "file": "postgresdriver.py",
            "qualified_function": "PostgresDriver.doStockLevel",
            "function": "doStockLevel",
        }
    }
    assert analyze_transformation(str(tmp_path), str(tmp_path), contract, {}) is None


def test_analyze_transformation_end_to_end(tmp_path):
    target_dir = tmp_path / "target"
    sandbox_dir = tmp_path / "sandbox"
    target_dir.mkdir()
    sandbox_dir.mkdir()

    original = _driver_source()
    (target_dir / "postgresdriver.py").write_text(original, encoding="utf-8")

    ok, rewritten, err = replace_function_ast(
        original, "PostgresDriver.doStockLevel", REWRITTEN_STOCK_LEVEL
    )
    assert ok, err
    (sandbox_dir / "postgresdriver.py").write_text(rewritten, encoding="utf-8")

    read_write_map = build_read_write_map(analyze_file(TPCC_DRIVER))
    contract = {
        "target": {
            "file": "postgresdriver.py",
            "qualified_function": "PostgresDriver.doStockLevel",
            "function": "doStockLevel",
        }
    }

    report = analyze_transformation(
        str(target_dir), str(sandbox_dir), contract, read_write_map
    )
    assert report is not None
    assert report.risk == "HIGH"
