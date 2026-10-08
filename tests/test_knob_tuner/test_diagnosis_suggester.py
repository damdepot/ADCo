"""Diagnosis agent is a knob improvement suggester grounded in evaluation facts.

Hermetic: no DB, no LLM. Pins the suggester framing of
``DIAGNOSIS_AGENT_PROMPT`` while keeping schema compat (``DiagnosisOutput``
still accepts all six ``CorrectionType`` values, including ``stop``).
"""

from src.knob_tuner.stages.models import CorrectionType, DiagnosisOutput
from src.knob_tuner.sub_agents.diagnosis_agent import prompt as diag_prompt_mod

_SUGGESTION_TAXONOMY = (
    "adjust_value",
    "drop_knob",
    "shrink_set",
    "change_phase",
    "retry_same",
)


def _taxonomy_values() -> set[str]:
    for line in diag_prompt_mod.DIAGNOSIS_AGENT_PROMPT.splitlines():
        if "Suggestion taxonomy" in line:
            return {
                value.strip().strip(".")
                for value in line.rsplit(":", 1)[-1].split(",")
                if value.strip().strip(".")
            }
    raise AssertionError("prompt has no 'Suggestion taxonomy' line")


def test_prompt_frames_suggester_role():
    text = diag_prompt_mod.DIAGNOSIS_AGENT_PROMPT
    assert "KNOB IMPROVEMENT SUGGESTER" in text
    assert "sole job" in text.lower()
    assert "never halt" in text.lower()


def test_prompt_requires_fact_citation():
    lowered = diag_prompt_mod.DIAGNOSIS_AGENT_PROMPT.lower()
    assert "no fact, no suggestion" in lowered
    for fact in ("mean", "lcb", "ucb", "status", "winners line", "knob beliefs"):
        assert fact in lowered, f"prompt must cite {fact!r} as grounding facts"


def test_prompt_has_no_stop_vs_next_winner_rule():
    text = diag_prompt_mod.DIAGNOSIS_AGENT_PROMPT
    assert "STOP-vs-NEXT" not in text
    assert 'Set `stop_reason` to "winner"' not in text


def test_prompt_taxonomy_lists_exactly_five_non_stop_values():
    assert _taxonomy_values() == set(_SUGGESTION_TAXONOMY)


def test_prompt_instructs_constant_stop_reason_futility():
    lowered = diag_prompt_mod.DIAGNOSIS_AGENT_PROMPT.lower()
    assert 'always emit "futility"' in lowered


def test_diagnosis_output_accepts_all_six_enum_values():
    for correction in CorrectionType:
        out = DiagnosisOutput(
            correction=correction,
            targets=[],
            rationale="compat",
            confidence=0.5,
        )
        assert out.correction is correction
    assert {c.value for c in CorrectionType} == set(_SUGGESTION_TAXONOMY) | {"stop"}
