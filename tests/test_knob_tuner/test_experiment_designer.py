"""Tests for the single-experiment proposal models.

The retired ``knob_recommender`` package (``ExperimentProposal``) was
superseded by ``candidate_generator``. What remains are the model-shape
tests for the live successor types in ``src.knob_tuner.stages.models``:
``CandidateProposal`` + ``ProposedLevel``. The candidate_generator
pure-agent coverage (tools == {get_knob_strategies, read_knob_details},
no write_* strings in prompts) lives in test_pure_agents.py and is not
duplicated here.
"""

from src.knob_tuner.stages.models import (
    CandidateProposal,
    ProposedLevel,
)


def _arm(name="a1", phase="screen", levels=None):
    levels = levels if levels is not None else [
        {"knob": "shared_buffers", "value": "512MB", "reasoning": "r"}
    ]
    return {"name": name, "phase": phase, "levels": levels, "rationale": "test"}


# Models: the single-experiment proposal shape.


def test_proposal_model_shape():
    lvl = ProposedLevel(knob="work_mem", value="64MB", reasoning="sort fit")
    prop = CandidateProposal(
        objective="o", name="screen_1", phase="screen",
        levels=[lvl], rationale="mover sweep",
    )
    assert prop.name == "screen_1"
    assert prop.phase == "screen"
    assert prop.levels[0].knob == "work_mem"
    assert set(ProposedLevel.model_fields) == {"knob", "value", "reasoning"}
    assert set(CandidateProposal.model_fields) == {
        "objective", "name", "phase", "levels", "rationale",
        "phase_raw", "repaired"}
    # Defaults mirror spec.
    assert CandidateProposal(
        name="n", phase="screen", levels=[lvl]).objective == ""
    assert CandidateProposal(
        name="n", phase="screen", levels=[lvl]).rationale == ""
