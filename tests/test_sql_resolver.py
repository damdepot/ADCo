import ast

from src.code_rewriter.tools.sql_resolver import (
    SENTINEL,
    collect_module_dicts,
    resolve,
)


def _expr(source: str) -> ast.AST:
    return ast.parse(source, mode="eval").body


def test_collect_module_dicts_and_subscript():
    source = '''
TXN_QUERIES = {"DELIVERY": {"getNewOrder": "SELECT NO_O_ID FROM NEW_ORDER"}}
'''
    module_dicts = collect_module_dicts(ast.parse(source))
    assert module_dicts["TXN_QUERIES"]["DELIVERY"]["getNewOrder"] == (
        "SELECT NO_O_ID FROM NEW_ORDER"
    )
    resolved = resolve(_expr('TXN_QUERIES["DELIVERY"]["getNewOrder"]'), {}, module_dicts)
    assert resolved == "SELECT NO_O_ID FROM NEW_ORDER"


def test_resolve_dict_alias():
    source = 'TXN_QUERIES = {"DELIVERY": {"getNewOrder": "SELECT 1"}}'
    module_dicts = collect_module_dicts(ast.parse(source))

    alias = resolve(_expr('TXN_QUERIES["DELIVERY"]'), {}, module_dicts)
    assert alias == {"getNewOrder": "SELECT 1"}

    resolved = resolve(_expr('q["getNewOrder"]'), {"q": alias}, module_dicts)
    assert resolved == "SELECT 1"


def test_resolve_percent_formatting():
    node = _expr('"SELECT a FROM t WHERE a = %s" % (x,)')
    assert resolve(node, {}, {}) == "SELECT a FROM t WHERE a = 0"


def test_resolve_replace():
    node = _expr('"SELECT a WHERE b = %s".replace("%s", "?")')
    assert resolve(node, {}, {}) == "SELECT a WHERE b = ?"


def test_resolve_joined_str_placeholder():
    node = _expr('f"SELECT {x}"')
    assert resolve(node, {}, {}) == "SELECT 0"


def test_unresolved_returns_none():
    assert resolve(_expr("unknown_var"), {}, {}) is None
    assert resolve(_expr('unknown_var["k"]'), {}, {}) is None
    assert resolve(None, {}, {}) is None


def test_partial_concat_yields_sentinel():
    node = _expr('"a" + unknown_var')
    assert resolve(node, {}, {}) == "a" + SENTINEL
