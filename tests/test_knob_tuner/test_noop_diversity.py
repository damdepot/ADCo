"""No-op dropout + family-diversity steering (hermetic, no DB)."""

from src.knob_tuner.stages import nodes
from src.knob_tuner.stages.evidence import build_evidence_bundle
from src.knob_tuner.stages.models import CompiledPlan, CompileRejection
from src.knob_tuner.stages.nodes.stats_coercion import (
    _diversity_dropout_suffix,
    _noop_drop_names,
)
from src.knob_tuner.sub_agents.candidate_generator.prompt import (
    CANDIDATE_GENERATOR_PROMPT,
)
from tests.test_knob_tuner.conftest import AdkCtx


def _entry(name, current, category, vartype="integer", enumvals=None):
    return {
        "name": name,
        "current_value": current,
        "unit": "",
        "vartype": vartype,
        "enumvals": list(enumvals or []),
        "context": "user",
        "category": category,
    }


def _ctx(knobs_info, winners=None):
    ctx = AdkCtx()
    ctx.state.update(
        {
            "knobs_info": knobs_info,
            "resource_budget": {"cpu_cores": 4, "memory_gb": 8.0},
            "durability_profile": "strict",
            "max_set_knobs": 20,
            "max_attempts": 6,
            "success_candidates": 1,
        }
    )
    if winners is not None:
        ctx.state.update({"winners": winners})
    return ctx


def _proposal(name="exp-1", levels=None):
    return {
        "name": name,
        "phase": "screen",
        "levels": levels
        if levels is not None
        else [{"knob": "mem_a", "value": "64MB"}],
    }


def _full_inventory():
    return [
        _entry("mem_a", "4MB", "memory"),
        _entry("mem_b", "8MB", "memory"),
        _entry("vac_a", "on", "autovacuum", vartype="bool",
               enumvals=["on", "off"]),
        _entry("vac_b", "20", "autovacuum"),
        _entry("vac_c", "0.2", "autovacuum"),
    ]


def test_noop_drop_persists_reasons_on_compiled_plan():
    ctx = _ctx(_full_inventory())
    out = nodes.compile_candidate(
        ctx,
        _proposal(
            levels=[
                {"knob": "vac_a", "value": "on"},  # no-op, dropped
                {"knob": "mem_a", "value": "64MB"},  # valid
            ]
        ),
    )
    assert isinstance(out, CompiledPlan)
    assert out.valid_knobs == ["mem_a"]
    assert any(
        "vac_a" in note and "no-op" in note for note in out.dropped_knobs
    )


def test_diversity_rejection_names_dropped_knob_and_suggests_alternatives():
    ctx = _ctx(
        _full_inventory(),
        winners=[{"knobs": [{"name": "mem_a", "value": "64MB"}]}],
    )
    out = nodes.compile_candidate(
        ctx,
        _proposal(
            levels=[
                {"knob": "vac_a", "value": "on"},  # no-op, dropped
                {"knob": "mem_a", "value": "64MB"},  # surviving, covered family
            ]
        ),
    )
    assert isinstance(out, CompileRejection)
    assert "family-diversity" in out.reason
    assert "vac_a" in out.reason  # dropped knob named
    # Same-family (autovacuum) alternatives suggested, capped at 3.
    assert "vac_b" in out.reason or "vac_c" in out.reason
    assert any("vac_b" in e or "vac_c" in e for e in out.errors)
    assert any("no-op" in e for e in out.errors)  # one-line warning for the LLM


def test_no_inventory_or_alternatives_message_unchanged_never_throws():
    # Only one autovacuum knob exists and it is the dropped one: no
    # suggestible alternative -> original message unchanged, no throw.
    ctx = _ctx(
        [
            _entry("mem_a", "4MB", "memory"),
            _entry("mem_b", "8MB", "memory"),
            _entry("vac_a", "on", "autovacuum", vartype="bool",
                   enumvals=["on", "off"]),
        ],
        winners=[{"knobs": [{"name": "mem_a", "value": "64MB"}]}],
    )
    out = nodes.compile_candidate(
        ctx,
        _proposal(
            levels=[
                {"knob": "vac_a", "value": "on"},
                {"knob": "mem_a", "value": "64MB"},
            ]
        ),
    )
    assert isinstance(out, CompileRejection)
    assert "family-diversity" in out.reason
    assert "note: proposed knob" not in out.reason
    # Helpers never throw on garbage.
    assert _diversity_dropout_suffix(None, None, None, None) == ("", "")
    assert _diversity_dropout_suffix({}, {}, {}, ["junk"]) == ("", "")
    assert _noop_drop_names(None) == []
    assert _noop_drop_names("junk") == []


def test_prompt_contains_noop_steering_bullet():
    text = CANDIDATE_GENERATOR_PROMPT.lower()
    assert "no-op" in text or "noop" in text or "current value" in text
    assert "same family" in text
    assert "re-proposed" in text or "repropos" in text


def test_dropped_notes_reach_evidence_bundle():
    ctx = _ctx(
        _full_inventory(),
        winners=[{"knobs": [{"name": "mem_a", "value": "64MB"}]}],
    )
    out = nodes.compile_candidate(
        ctx,
        _proposal(
            levels=[
                {"knob": "vac_a", "value": "on"},
                {"knob": "mem_a", "value": "64MB"},
            ]
        ),
    )
    assert isinstance(out, CompileRejection)
    state = {
        "experiment_history": [],
        "rejected_history": [out.reason, *out.errors],
        "last_rejection": out.model_dump(),
        "resource_budget": {"cpu_cores": 4, "memory_gb": 8},
        "validation_attempt_count": 1,
        "max_attempts": 6,
    }
    bundle = build_evidence_bundle(state)
    assert "vac_a" in bundle
    assert "vac_b" in bundle or "vac_c" in bundle
