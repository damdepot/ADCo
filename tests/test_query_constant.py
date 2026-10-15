"""Tests for the SINGLE_QUERY constant helpers."""

from src.code_rewriter.tools.query_constant import (
    constant_ast_dump,
    get_class_constant,
    owning_class_name,
    referenced_query_constant,
    replace_class_constant,
    target_constant,
)

APP = '''\
import psycopg2
from psycopg2.extras import RealDictCursor


class DatabaseClient:
    QUERY_1 = """\\
SELECT COUNT(*) FROM badges;
"""

    def run_query1(self):
        with self._conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(self.QUERY_1)
            results = cur.fetchall()
            return results


def main():
    with DatabaseClient() as client:
        print(client.run_query1())
'''


def test_owning_class_name():
    assert owning_class_name(APP, "DatabaseClient.run_query1") == "DatabaseClient"
    assert owning_class_name(APP, "run_query1") == "DatabaseClient"
    assert owning_class_name(APP, "main") is None


def test_referenced_query_constant():
    from src.code_rewriter._common import extract_function_source_by_name

    fn = extract_function_source_by_name(APP, "DatabaseClient.run_query1")
    assert referenced_query_constant(fn) == "QUERY_1"
    assert referenced_query_constant("def f(self):\n    cur.execute(sql)\n") is None


def test_get_class_constant_plain_and_non_plain():
    assert get_class_constant(APP, "DatabaseClient", "QUERY_1") == (
        True,
        "SELECT COUNT(*) FROM badges;\n",
    )
    assert get_class_constant(APP, "DatabaseClient", "MISSING") == (False, None)
    non_plain = "class C:\n    Q = 'a' + 'b'\n"
    assert get_class_constant(non_plain, "C", "Q") == (True, None)


def test_target_constant():
    assert target_constant(APP, "DatabaseClient.run_query1") == (
        "QUERY_1",
        True,
        "SELECT COUNT(*) FROM badges;\n",
    )


def test_replace_class_constant_roundtrip():
    ok, new_source, err = replace_class_constant(
        APP, "DatabaseClient", "QUERY_1", "SELECT COUNT(*) FROM users;"
    )
    assert ok, err
    assert get_class_constant(new_source, "DatabaseClient", "QUERY_1") == (
        True,
        "SELECT COUNT(*) FROM users;",
    )
    # Everything else is preserved.
    assert "def main():" in new_source
    assert constant_ast_dump(new_source, "DatabaseClient", "QUERY_1") is not None


def test_replace_class_constant_rejects_missing():
    ok, _, err = replace_class_constant(APP, "DatabaseClient", "NOPE", "x")
    assert not ok
    assert "not found" in err
