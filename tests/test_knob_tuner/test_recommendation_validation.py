"""Tests for the recommendation trust boundary in the tune loop."""

from src.knob_tuner.workflow import _validate_recommendations


def _entry(name, current_value, unit="", vartype="integer", context="user", enumvals=None):
    return {
        "name": name,
        "current_value": current_value,
        "unit": unit,
        "vartype": vartype,
        "context": context,
        "enumvals": enumvals or [],
    }


def test_rejects_value_equal_to_current_value():
    inventory = {"effective_cache_size": _entry("effective_cache_size", "524288", unit="8kB")}

    valid, rejected = _validate_recommendations(
        [{"name": "effective_cache_size", "value": "4GB"}],
        inventory,
        "strict",
    )

    assert valid == []
    assert len(rejected) == 1
    assert "no-op" in rejected[0]


def test_accepts_value_that_actually_changes():
    inventory = {"work_mem": _entry("work_mem", "4MB", unit="kB")}

    valid, rejected = _validate_recommendations(
        [{"name": "work_mem", "value": "256MB"}],
        inventory,
        "strict",
    )

    assert rejected == []
    assert valid == [{"name": "work_mem", "value": "256MB"}]


def test_unknown_units_are_not_treated_as_noop():
    inventory = {"mystery_knob": _entry("mystery_knob", "weird", unit="furlongs")}

    valid, rejected = _validate_recommendations(
        [{"name": "mystery_knob", "value": "other"}],
        inventory,
        "strict",
    )

    assert rejected == []
    assert len(valid) == 1
