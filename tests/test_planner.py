"""Tests for rewriter.tools.planner."""

from src.code_rewriter.tools.planner import _parse_kb, plan, _clean_name

def test_parse_kb_returns_30_strategies():
    strategies = _parse_kb()
    assert len(strategies) == 30

def test_top_5_strategies():
    strategies = _parse_kb()
    top_names = [_clean_name(s.name) for s in strategies[:5]]
    expected = ["COMBINING_QUERIES", "N_PLUS_ONE_QUERY_ELIMINATION", "QUERY_BATCHING", "REDUNDANT_QUERY_ELIMINATION", "PREDICATE_PUSHDOWN"]
    assert top_names == expected

def test_plan_n_plus_one():
    selected, summary = plan("sequential N+1 loop queries")
    names = [_clean_name(s.name) for s in selected]
    assert "N_PLUS_ONE_QUERY_ELIMINATION" in names or "COMBINING_QUERIES" in names

def test_plan_concurrency_for_async():
    selected, summary = plan("async concurrent connections")
    names = [_clean_name(s.name) for s in selected]
    assert "INDEPENDENT_QUERY_PARALLELISM" in names or "QUERY_BATCHING" in names

def test_plan_empty_string_returns_top_level():
    selected, summary = plan("")
    names = [_clean_name(s.name) for s in selected]
    assert len(names) > 0

def test_clean_name_strips_number_prefixes():
    assert _clean_name("1. COMBINING_QUERIES") == "COMBINING_QUERIES"

def test_parse_kb_multiline_mechanisms_and_risks():
    strategies = _parse_kb()
    assert len(strategies) > 0
    strat = strategies[0]
    assert "\n" in strat.mechanisms
    assert "\n" in strat.risks

def test_strategy_def_detailed_contains_risks_and_safety():
    strategies = _parse_kb()
    detailed_0 = strategies[0].detailed()
    assert "**Risks**:" in detailed_0
    detailed_9 = strategies[9].detailed()
    assert "**Safety**:" in detailed_9
