"""Unit tests for the knowledge-base planner tool."""

import os
import pytest

from src.knob_tuner.tools.kb_planner import (
    KnobStrategyDef,
    _parse_knob_kb,
    _calculate_strategy_score,
    plan_knob_tuning,
    get_knob_strategies,
    KB_PATH,
)


class MockToolContext:
    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}


def test_parse_knob_kb_returns_all_strategies():
    strategies = _parse_knob_kb()
    assert len(strategies) >= 14
    engines = {s.engine.lower() for s in strategies}
    assert any("postgresql" in e for e in engines)
    assert any("mysql" in e for e in engines)


def test_parse_knob_kb_target_engine_filter():
    pg_strategies = _parse_knob_kb(target_engine="postgresql")
    assert all("postgresql" in s.engine.lower() for s in pg_strategies)
    assert len(pg_strategies) >= 7

    mysql_strategies = _parse_knob_kb(target_engine="mysql")
    assert all("mysql" in s.engine.lower() for s in mysql_strategies)
    assert len(mysql_strategies) >= 7


def test_plan_knob_tuning_postgres_workload_scoring():
    # memory/read-heavy workload should boost shared memory
    strats, summary = plan_knob_tuning("postgres", workload_text="heavy read and cache workload")
    names = [s.name for s in strats]
    assert any("PG_SHARED_MEMORY_MANAGEMENT" == n for n in names[:3])
    assert "PG_SHARED_MEMORY_MANAGEMENT" in summary

    # autovacuum should be boosted
    strats, summary = plan_knob_tuning("postgres", workload_text="heavy update delete churn vacuum")
    names = [s.name for s in strats]
    assert any("PG_AUTOVACUUM_BACKGROUND_MAINTENANCE" == n for n in names[:3])


def test_plan_knob_tuning_mysql_workload_scoring():
    # buffer pool should be boosted
    strats, summary = plan_knob_tuning("mysql", workload_text="heavy cache and memory pool workload")
    names = [s.name for s in strats]
    assert any("MYSQL_GLOBAL_BUFFER_POOL_MANAGEMENT" == n for n in names[:3])
    assert "MYSQL_GLOBAL_BUFFER_POOL_MANAGEMENT" in summary

    # redo log / write
    strats, summary = plan_knob_tuning("mysql", workload_text="heavy write transaction commit redo")
    names = [s.name for s in strats]
    assert any("MYSQL_REDO_LOGGING_AND_TRANSACTION_DURABILITY" == n for n in names[:3])


def test_plan_knob_tuning_remediation_boost_on_feedback():
    strats, summary = plan_knob_tuning("postgres", feedback="Database service crash with fatal Out Of Memory (OOM)")
    names = [s.name for s in strats]
    assert names[0] == "PG_CHECKER_REMEDIATION_AND_FAILURE_RECOVERY"
    assert "PG_CHECKER_REMEDIATION_AND_FAILURE_RECOVERY" in summary


def test_plan_knob_tuning_fallback_baseline():
    strats, summary = plan_knob_tuning("postgres", workload_text="")
    assert len(strats) > 0
    names = [s.name for s in strats]
    assert any("MEMORY" in n or "WAL" in n for n in names)


def test_get_knob_strategies_tool_context():
    tc = MockToolContext({
        "db_type": "postgres",
        "workload": "read-heavy OLTP with connection pooling",
        "memory_gb": 8.0,
        "cpu_cores": 4,
        "knob_checker_output": "",
    })
    res = get_knob_strategies(tc)
    assert isinstance(res, str)
    assert len(res) > 0
    assert tc.state.get("knob_strategies") == res
    assert "PG_SHARED_MEMORY_MANAGEMENT" in res or "PG_CONCURRENCY" in res


def test_knob_strategy_def_detailed_format():
    strat = KnobStrategyDef(
        category="Memory Management",
        name="PG_SHARED_MEMORY_MANAGEMENT",
        engine="PostgreSQL",
        knobs="shared_buffers, effective_cache_size",
        definition="Configures shared buffer pool cache",
        objective="Maximize cache hit ratio",
        formulas="shared_buffers: 25% of RAM",
        conditions="Read-heavy workloads",
        restart_required="Static",
        risk_and_guardrails="Too high causes OOM",
    )
    detailed = strat.detailed()
    assert "### PG_SHARED_MEMORY_MANAGEMENT" in detailed
    assert "**Engine**: PostgreSQL" in detailed
    assert "**Category**: Memory Management" in detailed
    assert "**Knobs**: shared_buffers, effective_cache_size" in detailed
    assert "**Formulas / Baseline**: shared_buffers: 25% of RAM" in detailed


def test_calculate_strategy_score_performance_regression_keywords():
    mem_strat = KnobStrategyDef(
        category="Memory Management",
        name="PG_SHARED_MEMORY_MANAGEMENT",
        engine="PostgreSQL",
        knobs="shared_buffers",
        definition="Memory pool",
        objective="Cache data",
        formulas="25%",
        conditions="",
        restart_required="Static",
        risk_and_guardrails="",
    )
    remediation_strat = KnobStrategyDef(
        category="Remediation",
        name="PG_CHECKER_REMEDIATION_AND_FAILURE_RECOVERY",
        engine="PostgreSQL",
        knobs="shared_buffers",
        definition="Remediation",
        objective="Recover",
        formulas="stepdown",
        conditions="",
        restart_required="Static",
        risk_and_guardrails="",
    )

    perf_keywords = ["regression", "performance", "tps", "throughput", "latency"]
    for kw in perf_keywords:
        feedback = f"Detected {kw} issue during validation"
        score_mem = _calculate_strategy_score(mem_strat, feedback_lower=feedback)
        score_rem = _calculate_strategy_score(remediation_strat, feedback_lower=feedback)

        # Baseline without feedback is 1 for mem_strat and 0 for remediation
        base_mem = _calculate_strategy_score(mem_strat, feedback_lower="")
        base_rem = _calculate_strategy_score(remediation_strat, feedback_lower="")

        assert score_mem > base_mem, f"Expected boost for {kw} on engine performance tuning strategy"
        assert score_rem > base_rem, f"Expected boost for {kw} on remediation strategy"


def test_plan_knob_tuning_performance_regression_prioritization():
    feedback = "sysbench benchmark failed: tuned TPS (120) < baseline TPS (250), performance regression detected"
    strats, summary = plan_knob_tuning("postgres", feedback=feedback)
    names = [s.name for s in strats]

    # Remediation and performance tuning strategies must be in top results
    assert "PG_CHECKER_REMEDIATION_AND_FAILURE_RECOVERY" in names[:3]
    assert any(n in names[:3] for n in ["PG_SHARED_MEMORY_MANAGEMENT", "PG_WAL_CHECKPOINTING_AND_DURABILITY", "PG_CONCURRENCY_AND_PARALLEL_WORKERS"])
    assert "PG_CHECKER_REMEDIATION_AND_FAILURE_RECOVERY" in summary
