"""Unit tests for the pure knob coercion/plan helpers."""

import json
from types import SimpleNamespace

from src.knob_tuner.contracts import (
    ApplyMode,
    KnobScope,
    KnobSpec,
    SysbenchProfile,
)
from src.knob_tuner.tools.db_connector import DBConfig
from src.knob_tuner.tools.knobs import (
    build_plan,
    coerce_apply_mode,
    coerce_db_config,
    coerce_profile,
    dedupe_by_name,
    extract_knob_list,
    load_raw_knobs,
    normalize_knob,
)


# ---------------------------------------------------------------------------
# normalize_knob
# ---------------------------------------------------------------------------


def test_normalize_knob_name_value():
    result = normalize_knob(
        {
            "name": "shared_buffers",
            "value": "256MB",
            "restart_required": True,
            "reasoning": "cache",
        }
    )
    assert result == {
        "name": "shared_buffers",
        "value": "256MB",
        "restart_required": True,
        "reasoning": "cache",
    }


def test_normalize_knob_knob_recommended_value():
    result = normalize_knob({"knob": "max_connections", "recommended_value": 200})
    assert result is not None
    assert result["name"] == "max_connections"
    assert result["value"] == 200
    assert result["restart_required"] is False
    assert result["reasoning"] == ""


def test_normalize_knob_pydantic_model_dump():
    spec = KnobSpec(
        name="work_mem",
        value="8MB",
        scope=KnobScope.USER,
        restart_required=False,
        reasoning="per-sort",
    )
    result = normalize_knob(spec)
    assert result == {
        "name": "work_mem",
        "value": "8MB",
        "restart_required": False,
        "reasoning": "per-sort",
    }


def test_normalize_knob_nameless_returns_none():
    assert normalize_knob({"value": "256MB"}) is None
    assert normalize_knob("not-a-knob") is None
    assert normalize_knob(None) is None


def test_normalize_knob_preserves_restart_required_and_reasoning():
    result = normalize_knob(
        {"name": "max_wal_size", "value": "2GB", "restart_required": True}
    )
    assert result["restart_required"] is True
    assert result["reasoning"] == ""


# ---------------------------------------------------------------------------
# extract_knob_list
# ---------------------------------------------------------------------------


def test_extract_knob_list_list_of_dicts():
    result = extract_knob_list(
        [{"name": "a", "value": 1}, {"knob": "b", "recommended_value": 2}]
    )
    assert [item["name"] for item in result] == ["a", "b"]


def test_extract_knob_list_recommendations_dict():
    result = extract_knob_list(
        {"recommendations": [{"name": "a", "value": 1}]}
    )
    assert [item["name"] for item in result] == ["a"]


def test_extract_knob_list_selected_knobs_dict():
    result = extract_knob_list({"selected_knobs": [{"name": "b", "value": 2}]})
    assert [item["name"] for item in result] == ["b"]


def test_extract_knob_list_recommendations_attribute():
    obj = SimpleNamespace(recommendations=[{"name": "c", "value": 3}])
    result = extract_knob_list(obj)
    assert [item["name"] for item in result] == ["c"]


def test_extract_knob_list_none():
    assert extract_knob_list(None) == []


# ---------------------------------------------------------------------------
# dedupe_by_name
# ---------------------------------------------------------------------------


def test_dedupe_by_name_keeps_first_preserves_order():
    items = [
        {"name": "a", "value": 1},
        {"name": "b", "value": 2},
        {"name": "a", "value": 99},
        {"name": "c", "value": 3},
    ]
    result = dedupe_by_name(items)
    assert [item["name"] for item in result] == ["a", "b", "c"]
    assert result[0]["value"] == 1


# ---------------------------------------------------------------------------
# load_raw_knobs
# ---------------------------------------------------------------------------


def test_load_raw_knobs_from_selected_knobs():
    state = {"selected_knobs": [{"name": "a", "value": 1}]}
    assert [item["name"] for item in load_raw_knobs(state)] == ["a"]


def test_load_raw_knobs_from_recommender_output():
    state = {"knob_recommender_output": {"recommendations": [{"name": "b", "value": 2}]}}
    assert [item["name"] for item in load_raw_knobs(state)] == ["b"]


def test_load_raw_knobs_falls_back_to_file(tmp_path):
    (tmp_path / "knobs-selected.json").write_text(
        json.dumps([{"name": "from_file", "value": 7}]), encoding="utf-8"
    )
    state = {"knob_path": str(tmp_path)}
    assert [item["name"] for item in load_raw_knobs(state)] == ["from_file"]


def test_load_raw_knobs_dedupes_combined_candidates():
    state = {
        "selected_knobs": [{"name": "a", "value": 1}],
        "knob_recommender_output": {
            "recommendations": [{"name": "a", "value": 99}, {"name": "b", "value": 2}]
        },
    }
    result = load_raw_knobs(state)
    assert [item["name"] for item in result] == ["a", "b"]
    assert result[0]["value"] == 1


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def test_build_plan_sets_restart_required_for_postmaster():
    raw_knobs = [
        {"name": "shared_buffers", "value": "1GB"},
        {"name": "work_mem", "value": "8MB"},
    ]
    context_map = {"shared_buffers": "sighup", "work_mem": "postmaster"}

    plan = build_plan(raw_knobs, context_map)

    shared = next(k for k in plan.knobs if k.name == "shared_buffers")
    work_mem = next(k for k in plan.knobs if k.name == "work_mem")
    assert shared.scope == KnobScope.SIGHUP
    assert shared.restart_required is False
    assert work_mem.scope == KnobScope.POSTMASTER
    assert work_mem.restart_required is True


def test_build_plan_preserves_restart_required_on_unknown_scope():
    raw_knobs = [
        {"name": "restart_me", "value": 1, "restart_required": True},
        {"name": "leave_me", "value": 2},
    ]

    plan = build_plan(raw_knobs, {})

    restart_me = next(k for k in plan.knobs if k.name == "restart_me")
    leave_me = next(k for k in plan.knobs if k.name == "leave_me")
    assert restart_me.scope == KnobScope.UNKNOWN
    assert restart_me.restart_required is True
    assert leave_me.restart_required is False


# ---------------------------------------------------------------------------
# coerce_profile
# ---------------------------------------------------------------------------


def test_coerce_profile_dict():
    profile = coerce_profile({"threads": 8, "seed": 7})
    assert isinstance(profile, SysbenchProfile)
    assert profile.threads == 8
    assert profile.seed == 7


def test_coerce_profile_invalid_dict_returns_defaults():
    profile = coerce_profile({"tables": 0})
    assert profile == SysbenchProfile()


def test_coerce_profile_non_dict_returns_defaults():
    assert coerce_profile("nope") == SysbenchProfile()
    assert coerce_profile(None) == SysbenchProfile()


def test_coerce_profile_passthrough_instance():
    existing = SysbenchProfile(threads=16)
    assert coerce_profile(existing) is existing


# ---------------------------------------------------------------------------
# coerce_apply_mode
# ---------------------------------------------------------------------------


def test_coerce_apply_mode_enum_passthrough():
    assert coerce_apply_mode(ApplyMode.PERSIST_STATIC) == ApplyMode.PERSIST_STATIC


def test_coerce_apply_mode_string():
    assert coerce_apply_mode("persist-static") == ApplyMode.PERSIST_STATIC


def test_coerce_apply_mode_invalid_returns_dynamic():
    assert coerce_apply_mode("bogus") == ApplyMode.DYNAMIC


# ---------------------------------------------------------------------------
# coerce_db_config
# ---------------------------------------------------------------------------


def test_coerce_db_config_dict():
    cfg = coerce_db_config(
        {
            "host": "db.example.com",
            "port": 5433,
            "user": "admin",
            "password": "secret",
            "database": "app",
            "db_type": "postgres",
        }
    )
    assert isinstance(cfg, DBConfig)
    assert cfg.host == "db.example.com"
    assert cfg.port == 5433
    assert cfg.database == "app"


def test_coerce_db_config_none_returns_none():
    assert coerce_db_config(None) is None


def test_coerce_db_config_non_dict_returns_none():
    assert coerce_db_config("not-a-dict") is None
