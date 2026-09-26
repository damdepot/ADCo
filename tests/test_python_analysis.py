from src.code_rewriter.tools.python_analysis import (
    percent_format_arity_violations,
    slow_executemany_violations,
    undefined_name_violations,
)


def test_percent_correct_escaped_placeholders_not_flagged():
    source = '''
def build(d_id):
    template = "SELECT S_DIST_%02d FROM STOCK WHERE S_I_ID = %%s AND S_W_ID = %%s"
    return template % (d_id,)
'''
    assert percent_format_arity_violations(source) == []


def test_percent_interpolated_placeholders_flagged():
    source = '''
def build(d_id):
    stock_placeholders = "(%s, %s), (%s, %s)"
    sql = (f"SELECT S_DIST_%02d FROM STOCK WHERE (S_I_ID, S_W_ID) IN ({stock_placeholders})") % (d_id,)
    return sql
'''
    violations = percent_format_arity_violations(source)
    assert len(violations) == 1
    assert violations[0]["function"] == "build"
    assert violations[0]["expected"] == 5
    assert violations[0]["actual"] == 1
    assert violations[0]["expected"] > violations[0]["actual"]


def test_percent_fstring_dynamic_placeholders_flagged():
    source = '''
def build(cursor, d_id, stock_params):
    stock_placeholders = ",".join(["(%s, %s)"] * len(stock_params))
    sql = (f"SELECT S_DIST_%02d FROM STOCK WHERE (S_I_ID, S_W_ID) IN ({stock_placeholders})") % (d_id,)
    cursor.execute(sql)
'''
    violations = percent_format_arity_violations(source)
    assert len(violations) == 1
    assert violations[0]["function"] == "build"


def test_percent_plain_single_match_not_flagged():
    source = '''
def f(cursor, val):
    cursor.execute("WHERE a=%s" % (val,))
'''
    assert percent_format_arity_violations(source) == []


def test_percent_plain_mismatch_flagged():
    source = '''
def f(cursor, a):
    cursor.execute("x=%s y=%s" % (a,))
'''
    violations = percent_format_arity_violations(source)
    assert len(violations) == 1
    assert violations[0]["expected"] == 2
    assert violations[0]["actual"] == 1
    assert violations[0]["function"] == "f"


def test_percent_named_dict_match_not_flagged():
    source = '''
def f(cursor):
    cursor.execute("WHERE a=%(a)s AND b=%(b)s" % {"a": 1, "b": 2})
'''
    assert percent_format_arity_violations(source) == []


def test_percent_unresolvable_args_skipped():
    source = '''
def f(cursor, args):
    cursor.execute("x=%s y=%s" % args)
'''
    assert percent_format_arity_violations(source) == []


def test_percent_syntax_error():
    assert percent_format_arity_violations("def f(:") == []


def test_undefined_name_missing_binding_flagged():
    source = '''
def build(stock_rows):
    stock_map = {(row[0], row[1]): (row[2], row[3])}
    return stock_map
'''
    violations = undefined_name_violations(source)
    assert len(violations) == 1
    assert violations[0]["name"] == "row"
    assert violations[0]["function"] == "build"
    assert violations[0]["line"] is not None


def test_undefined_name_comprehension_bound_not_flagged():
    source = '''
def build(stock_rows):
    stock_map = {(row[0], row[1]): (row[2], row[3]) for row in stock_rows}
    return stock_map
'''
    assert undefined_name_violations(source) == []


def test_undefined_name_module_global_not_flagged():
    source = '''
STOCK_ROWS = []

def build():
    return STOCK_ROWS
'''
    assert undefined_name_violations(source) == []


def test_undefined_name_builtins_not_flagged():
    source = '''
def build(values):
    return len(list(range(len(values))))
'''
    assert undefined_name_violations(source) == []


def test_undefined_name_nested_def_name_bound():
    source = '''
def build(values):
    def helper(x):
        return x + 1
    return helper(len(values))
'''
    assert undefined_name_violations(source) == []


def test_undefined_name_star_import_skipped():
    source = '''
from os import *

def build():
    return path
'''
    assert undefined_name_violations(source) == []


def test_undefined_name_dynamic_scope_skipped():
    source = '''
def build():
    return eval("x")
'''
    assert undefined_name_violations(source) == []


def test_undefined_name_syntax_error():
    assert undefined_name_violations("def f(:") == []


def test_slow_executemany_flagged():
    source = '''
import psycopg2

def bulk_insert(cursor, rows):
    cursor.executemany("INSERT INTO t (a, b) VALUES (%s, %s)", rows)
'''
    violations = slow_executemany_violations(source)
    assert len(violations) == 1
    assert violations[0]["function"] == "bulk_insert"
    assert violations[0]["line"] is not None


def test_slow_executemany_with_fast_path_import_not_flagged():
    source = '''
import psycopg2
from psycopg2.extras import execute_batch

def bulk_insert(cursor, rows):
    cursor.executemany("INSERT INTO t (a, b) VALUES (%s, %s)", rows)
'''
    assert slow_executemany_violations(source) == []


def test_executemany_sqlite_not_flagged():
    source = '''
import sqlite3

def bulk_insert(cursor, rows):
    cursor.executemany("INSERT INTO t (a, b) VALUES (?, ?)", rows)
'''
    assert slow_executemany_violations(source) == []


def test_executemany_psycopg2_select_not_flagged():
    source = '''
import psycopg2

def fetch(cursor, rows):
    cursor.executemany("SELECT a FROM t WHERE b = %s", rows)
'''
    assert slow_executemany_violations(source) == []


def test_executemany_no_psycopg2_import_not_flagged():
    source = '''
def bulk_insert(cursor, rows):
    cursor.executemany("INSERT INTO t (a, b) VALUES (%s, %s)", rows)
'''
    assert slow_executemany_violations(source) == []


def test_slow_executemany_syntax_error():
    assert slow_executemany_violations("def f(:") == []
