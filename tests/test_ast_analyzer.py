import pytest
from src.code_rewriter.models.ast_models import SourceLocation
from src.code_rewriter.tools.ast_analyzer import analyze_source
from src.code_rewriter.tools.rewrite_contract import build_rewrite_contract
from src.code_rewriter.models.rewrite_models import RewriteTarget

def test_analyze_source_basic():
    source = """
import os
from sys import path as sys_path

class MyClass:
    def method(self, x):
        for i in range(10):
            pass
        return x
        
def function(y):
    db.execute("SELECT * FROM table")
    """
    
    analysis = analyze_source(source, "test.py")
    
    assert analysis.parse_success is True
    assert analysis.file_path == "test.py"
    assert len(analysis.imports) == 2
    assert analysis.structural_summary.class_count == 1
    assert analysis.structural_summary.function_count == 2
    assert analysis.structural_summary.for_loop_count == 1
    assert analysis.structural_summary.db_operation_count == 1
    
    db_ops = analysis.database_operations
    assert len(db_ops) == 1
    assert db_ops[0].call_name == "execute"
    assert db_ops[0].operation_type == "EXECUTE"
    assert db_ops[0].sql_operation == "SELECT"
    assert db_ops[0].sql == "SELECT * FROM table"

def test_analyze_source_syntax_error():
    source = "def broken("
    analysis = analyze_source(source)
    assert analysis.parse_success is False
    assert analysis.parse_error is not None

def test_analyze_imports():
    source = """
import math
import os.path as p
from typing import List, Optional as Opt
    """
    analysis = analyze_source(source)
    assert len(analysis.imports) == 4
    
    math_imp = analysis.imports[0]
    assert math_imp.import_type == "import"
    assert math_imp.name == "math"
    
    p_imp = analysis.imports[1]
    assert p_imp.name == "os.path"
    assert p_imp.alias == "p"
    
    list_imp = analysis.imports[2]
    assert list_imp.import_type == "from"
    assert list_imp.module == "typing"
    assert list_imp.name == "List"
    
    opt_imp = analysis.imports[3]
    assert opt_imp.name == "Optional"
    assert opt_imp.alias == "Opt"

def test_analyze_control_flow():
    source = """
def complex_func():
    for x in range(5):
        pass
    while True:
        break
    if True:
        pass
    try:
        with open('x') as f:
            pass
    except:
        pass
    a = [i for i in range(10)]
    """
    analysis = analyze_source(source)
    func = analysis.functions[0]
    cf = func.control_flow
    assert cf.for_loops == 1
    assert cf.while_loops == 1
    assert cf.conditionals == 1
    assert cf.try_blocks == 1
    assert cf.with_blocks == 1
    assert cf.comprehensions == 1

def test_analyze_function_calls():
    source = """
def func():
    print("hello")
    obj.method()
    """
    analysis = analyze_source(source)
    func = analysis.functions[0]
    
    assert len(func.calls) == 2
    assert func.calls[0].call_name == "print"
    assert func.calls[0].receiver is None
    assert func.calls[0].containing_function == "func"
    
    assert func.calls[1].call_name == "method"
    assert func.calls[1].receiver == "obj"

def test_db_operations():
    source = """
def db_ops():
    conn = db.connect()
    cur = conn.cursor()
    cur.execute("SELECT 1")
    cur.executemany("INSERT INTO t VALUES (%s)", [])
    cur.fetchone()
    cur.fetchmany(10)
    cur.fetchall()
    conn.commit()
    conn.rollback()
    """
    analysis = analyze_source(source)
    db_ops = analysis.database_operations
    assert len(db_ops) == 9
    op_types = [op.operation_type for op in db_ops]
    assert op_types == ["CONNECT", "CURSOR", "EXECUTE", "EXECUTEMANY", "FETCH", "FETCH", "FETCH", "COMMIT", "ROLLBACK"]

def test_sql_classification():
    source = """
def ops():
    cur.execute("select * from x")
    cur.execute("  INSERT into x")
    cur.execute("UPDATE x set")
    cur.execute("delete FROM x")
    cur.execute("CREATE TABLE")
    """
    analysis = analyze_source(source)
    db_ops = analysis.database_operations
    assert db_ops[0].sql_operation == "SELECT"
    assert db_ops[1].sql_operation == "INSERT"
    assert db_ops[2].sql_operation == "UPDATE"
    assert db_ops[3].sql_operation == "DELETE"
    assert db_ops[4].sql_operation == "OTHER"

def test_sql_dynamic_and_local_variables():
    source = """
GLOBAL_QUERY = "SELECT 1"

def f():
    q = "UPDATE table SET a = 1"
    cur.execute(q)
    cur.execute(GLOBAL_QUERY)
    cur.execute(f"SELECT {x}")
    """
    analysis = analyze_source(source)
    db_ops = analysis.database_operations
    
    assert db_ops[0].sql == "UPDATE table SET a = 1"
    assert db_ops[0].sql_operation == "UPDATE"
    
    assert db_ops[1].sql == "SELECT 1"
    assert db_ops[1].sql_operation == "SELECT"
    
    assert db_ops[2].sql is None
    assert db_ops[2].sql_operation == "UNKNOWN"

def test_db_operation_inside_loop_and_param_deps():
    source = """
def update_products():
    for product_id in ids:
        while True:
            cur.execute("UPDATE products SET price = 10 WHERE id = %s", (product_id,))
    """
    analysis = analyze_source(source)
    op = analysis.database_operations[0]
    
    assert op.inside_loop is True
    assert "product_id" in op.loop_variables
    assert "product_id" in op.parameter_dependencies

def test_non_db_loop():
    source = """
def f():
    for x in range(10):
        print(x)
    """
    analysis = analyze_source(source)
    assert len(analysis.database_operations) == 0

def test_serialization():
    source = "def f(): pass"
    analysis = analyze_source(source)
    data = analysis.model_dump()
    assert data["parse_success"] is True
    assert isinstance(data["functions"], list)
    assert data["functions"][0]["name"] == "f"

def test_end_to_end_fixture():
    source = """
class ProductRepository:
    def get_products(self, product_ids):
        results = []
        for product_id in product_ids:
            query = "SELECT * FROM products WHERE id = %s"
            cursor.execute(query, (product_id,))
            results.append(cursor.fetchone())
        return results
    """
    analysis = analyze_source(source, "repo.py")
    
    assert analysis.file_path == "repo.py"
    
    db_ops = analysis.database_operations
    assert len(db_ops) == 2
    
    exec_op = db_ops[0]
    assert exec_op.call_name == "execute"
    assert exec_op.sql == "SELECT * FROM products WHERE id = %s"
    assert exec_op.sql_operation == "SELECT"
    assert exec_op.inside_loop is True
    assert "product_id" in exec_op.loop_variables
    assert "product_id" in exec_op.parameter_dependencies
    
    fetch_op = db_ops[1]
    assert fetch_op.call_name == "fetchone"
    assert fetch_op.inside_loop is True
    
    target = RewriteTarget(file="repo.py", qualified_function="ProductRepository.get_products")
    contract = build_rewrite_contract(
        analysis=analysis,
        target=target,
        pattern="N+1 Query",
        strategy="Batch Fetching",
        must_preserve=["Return correct results"],
        must_not_change=["Table schema"]
    )
    
    assert contract.target.source_location is not None
    assert contract.pattern == "N+1 Query"
    assert contract.strategy == "Batch Fetching"
    assert contract.must_preserve == ["Return correct results"]
    assert contract.must_not_change == ["Table schema"]
