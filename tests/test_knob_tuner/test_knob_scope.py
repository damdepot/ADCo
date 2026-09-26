"""Unit tests for the knob scope classification helpers."""

import pytest

from src.knob_tuner.contracts import KnobScope
from src.knob_tuner.tools import knob_scope
from src.knob_tuner.tools.knob_scope import (
    build_knob_plan,
    classify_scope,
    fetch_pg_settings_context,
    scope_to_restart_required,
)


@pytest.mark.parametrize(
    "context,expected",
    [
        ("sighup", KnobScope.SIGHUP),
        ("SIGHUP", KnobScope.SIGHUP),
        ("user", KnobScope.USER),
        ("postmaster", KnobScope.POSTMASTER),
        ("internal", KnobScope.INTERNAL),
        ("superuser", KnobScope.UNKNOWN),
        ("", KnobScope.UNKNOWN),
        (None, KnobScope.UNKNOWN),
    ],
)
def test_classify_scope(context, expected):
    assert classify_scope(context) == expected


def test_scope_to_restart_required():
    assert scope_to_restart_required(KnobScope.POSTMASTER) is True
    assert scope_to_restart_required(KnobScope.SIGHUP) is False
    assert scope_to_restart_required(KnobScope.USER) is False
    assert scope_to_restart_required(KnobScope.INTERNAL) is False
    assert scope_to_restart_required(KnobScope.UNKNOWN) is False


def test_build_knob_plan_skips_internal_and_sets_restart():
    raw_knobs = [
        {"name": "shared_buffers", "value": "256MB", "reasoning": "cache"},
        {"knob": "max_connections", "recommended_value": 200},
        {"name": "wal_buffers", "value": "-1", "reasoning": "internal"},
    ]
    context_map = {
        "shared_buffers": "postmaster",
        "max_connections": "sighup",
        "wal_buffers": "internal",
    }

    plan = build_knob_plan(raw_knobs, context_map)

    names = [knob.name for knob in plan.knobs]
    assert names == ["shared_buffers", "max_connections"]

    shared = plan.knobs[0]
    assert shared.scope == KnobScope.POSTMASTER
    assert shared.restart_required is True
    assert shared.reasoning == "cache"

    max_conn = plan.knobs[1]
    assert max_conn.value == 200
    assert max_conn.scope == KnobScope.SIGHUP
    assert max_conn.restart_required is False


def test_build_knob_plan_context_lookup_is_case_insensitive():
    raw_knobs = [{"name": "Shared_Buffers", "value": "128MB"}]
    context_map = {"SHARED_BUFFERS": "Postmaster"}

    plan = build_knob_plan(raw_knobs, context_map)

    assert plan.knobs[0].scope == KnobScope.POSTMASTER
    assert plan.knobs[0].restart_required is True


def test_build_knob_plan_unknown_scope_when_context_missing():
    plan = build_knob_plan([{"name": "some_knob", "value": 1}], {})
    assert plan.knobs[0].scope == KnobScope.UNKNOWN
    assert plan.knobs[0].restart_required is False


def test_fetch_pg_settings_context_returns_map(monkeypatch):
    rows = [
        {"name": "shared_buffers", "context": "postmaster"},
        {"name": "work_mem", "context": "user"},
    ]
    monkeypatch.setattr(knob_scope, "run_safe_query", lambda cfg, sql: rows)

    assert fetch_pg_settings_context(object()) == {
        "shared_buffers": "postmaster",
        "work_mem": "user",
    }


def test_fetch_pg_settings_context_returns_empty_on_error(monkeypatch):
    def boom(cfg, sql):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(knob_scope, "run_safe_query", boom)

    assert fetch_pg_settings_context(object()) == {}
