"""Tests for rewriter.tools.planner."""

from src.rewriter.tools.planner import _parse_kb, plan, _clean_name


def test_parse_kb_returns_70_plus_strategies():
    strategies = _parse_kb()
    assert len(strategies) >= 70


def test_top_5_strategies():
    strategies = _parse_kb()
    top_names = [_clean_name(s.name) for s in strategies[:5]]
    expected = ["COMBINING_QUERIES", "PREDICATE_PUSHDOWN", "JOIN_ORDER_HINTS", "SEPARATING_QUERIES", "CONCURRENCY"]
    assert top_names == expected


def test_plan_combining_queries_for_n_plus_one():
    selected, summary = plan("sequential N+1 loop queries")
    names = [_clean_name(s.name) for s in selected]
    assert names[0] == "COMBINING_QUERIES"


def test_plan_concurrency_for_async():
    selected, summary = plan("async concurrent connections")
    names = [_clean_name(s.name) for s in selected]
    assert names[0] == "CONCURRENCY"


def test_plan_empty_string_returns_top_level():
    selected, summary = plan("")
    categories = {s.category for s in selected}
    assert categories == {"TOP_LEVEL"}
    names = [_clean_name(s.name) for s in selected]
    expected = ["COMBINING_QUERIES", "PREDICATE_PUSHDOWN", "JOIN_ORDER_HINTS", "SEPARATING_QUERIES", "CONCURRENCY"]
    assert names == expected


def test_clean_name_strips_number_prefixes():
    assert _clean_name("1. COMBINING_QUERIES") == "COMBINING_QUERIES"
    assert _clean_name("6.1. AGGREGATE_MERGE") == "AGGREGATE_MERGE"
    assert _clean_name("14.3. JOIN_SUB_QUERY_TO_CORRELATE / JOIN_TO_CORRELATE") == "JOIN_SUB_QUERY_TO_CORRELATE / JOIN_TO_CORRELATE"
    assert _clean_name("SIMPLE_NAME") == "SIMPLE_NAME"


def test_plan_returns_summary_text():
    selected, summary = plan("filter where pushdown early")
    assert isinstance(summary, str)
    assert len(summary) > 0
    names = [_clean_name(s.name) for s in selected]
    assert "PREDICATE_PUSHDOWN" in names
