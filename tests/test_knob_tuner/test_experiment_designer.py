"""Tests for knob_recommender as sequential single-experiment designer."""

import json
import os
import tempfile

from src.knob_tuner.sub_agents.knob_recommender.agent import (
    create_knob_recommender_agent,
)
from src.knob_tuner.sub_agents.knob_recommender.models import (
    DesignedArm,
    DesignedLevel,
    ExperimentDesignOutput,
    ExperimentProposal,
    KnobRecommendation,
    KnobRecommenderOutput,
    ProposedLevel,
)
from src.knob_tuner.sub_agents.knob_recommender.tools import (
    write_experiment_protocol,
    write_next_experiment,
)
from src.knob_tuner.tools.experiments import (
    ExperimentArm,
    ExperimentLevel,
    ExperimentProtocol,
)


class MockToolContext:
    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}


def _arm(name="a1", phase="screen", levels=None):
    levels = levels if levels is not None else [
        {"knob": "shared_buffers", "value": "512MB", "reasoning": "r"}
    ]
    return {"name": name, "phase": phase, "levels": levels, "rationale": "test"}


# Models: backward compat + new shapes mirror experiments.py

def test_legacy_models_untouched():
    rec = KnobRecommendation(
        knob="work_mem", current_value="4MB", recommended_value="32MB", reasoning="r"
    )
    out = KnobRecommenderOutput(recommendations=[rec], summary="s")
    assert out.recommendations[0].knob == "work_mem"


def test_design_models_mirror_protocol_shapes():
    lvl = DesignedLevel(knob="work_mem", value="64MB", reasoning="sort fit")
    arm = DesignedArm(name="screen_wm", phase="screen", levels=[lvl], rationale="mover")
    design = ExperimentDesignOutput(objective="o", arms=[arm], summary="s")
    assert design.arms[0].levels[0].knob == "work_mem"
    # Same field names as ExperimentLevel/ExperimentArm/ExperimentProtocol.
    assert set(DesignedLevel.model_fields) == set(ExperimentLevel.model_fields)
    assert set(DesignedArm.model_fields) == set(ExperimentArm.model_fields)
    assert set(ExperimentDesignOutput.model_fields) == set(ExperimentProtocol.model_fields)
    # Phase validity lives in tools, not the model: any string validates.
    assert DesignedArm(name="x", phase="bogus", levels=[lvl]).phase == "bogus"


def test_proposal_model_shape():
    lvl = ProposedLevel(knob="work_mem", value="64MB", reasoning="sort fit")
    prop = ExperimentProposal(
        objective="o", name="screen_1", phase="screen",
        levels=[lvl], rationale="mover sweep",
    )
    assert prop.name == "screen_1"
    assert prop.phase == "screen"
    assert prop.levels[0].knob == "work_mem"
    assert set(ProposedLevel.model_fields) == {"knob", "value", "reasoning"}
    assert set(ExperimentProposal.model_fields) == {
        "objective", "name", "phase", "levels", "rationale"}
    # Defaults mirror spec.
    assert ExperimentProposal(
        name="n", phase="screen", levels=[lvl]).objective == ""
    assert ExperimentProposal(
        name="n", phase="screen", levels=[lvl]).rationale == ""


# write_experiment_protocol: normalization inputs (batch compat kept)

def test_write_protocol_from_object_input():
    with tempfile.TemporaryDirectory() as tmpdir:
        design = ExperimentDesignOutput(
            objective="o",
            arms=[
                DesignedArm(name="s1", phase="Screen",
                            levels=[DesignedLevel(knob="work_mem", value="64MB")]),
                DesignedArm(name="i1", phase="interaction",
                            levels=[DesignedLevel(knob="max_wal_size", value="4GB")]),
            ],
        )
        tc = MockToolContext({"knob_path": tmpdir, "experiment_design_output": design})
        result = write_experiment_protocol(tc)
        assert "OK: wrote 2 arms (1 screen / 1 interaction / 0 refinement)" in result
        assert len(tc.state["experiment_protocol"]) == 2
        assert tc.state["experiment_protocol"][0]["phase"] == "screen"
        out_file = os.path.join(tmpdir, "experiment-protocol.json")
        assert os.path.isfile(out_file)
        with open(out_file) as f:
            assert len(json.load(f)) == 2


def test_write_protocol_from_dict_and_designs_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        tc = MockToolContext({
            "knob_path": tmpdir,
            # dict with "arms" key, alt knob/value key spellings.
            "experiment_design_output": {
                "arms": [{
                    "name": "r1", "phase": "REFINEMENT",
                    "levels": [{"name": "checkpoint_completion_target",
                                "recommended_value": "0.9"}],
                }]
            },
        })
        result = write_experiment_protocol(tc)
        assert "OK: wrote 1 arms (0 screen / 0 interaction / 1 refinement)" in result
        assert tc.state["experiment_protocol"][0]["levels"][0]["knob"] == \
            "checkpoint_completion_target"

    with tempfile.TemporaryDirectory() as tmpdir:
        tc = MockToolContext({
            "knob_path": tmpdir,
            # list under "designs".
            "experiment_design_output": {"designs": [_arm("s1", "screen")]},
        })
        result = write_experiment_protocol(tc)
        assert "OK: wrote 1 arms" in result


def test_write_protocol_invalid_phases_reported_and_excluded():
    with tempfile.TemporaryDirectory() as tmpdir:
        tc = MockToolContext({
            "knob_path": tmpdir,
            "experiment_design_output": {
                "arms": [_arm("good", "screen"), _arm("bad", "bogus-phase")]
            },
        })
        result = write_experiment_protocol(tc)
        assert "OK: wrote 1 arms" in result
        assert "bogus-phase" in result  # invalid reported in message
        assert [a["name"] for a in tc.state["experiment_protocol"]] == ["good"]
        with open(os.path.join(tmpdir, "experiment-protocol.json")) as f:
            assert len(json.load(f)) == 1


def test_write_protocol_clamps_memory():
    with tempfile.TemporaryDirectory() as tmpdir:
        tc = MockToolContext({
            "knob_path": tmpdir,
            "memory_gb": 2.0,  # 40% cap on shared_buffers = 0.8GB
            "experiment_design_output": {
                "arms": [_arm("s1", "screen", levels=[
                    {"knob": "shared_buffers", "value": "100GB"}])]
            },
        })
        result = write_experiment_protocol(tc)
        assert "OK: wrote 1 arms" in result
        assert tc.state["experiment_protocol"][0]["levels"][0]["value"] != "100GB"


def test_write_protocol_missing_design():
    tc = MockToolContext({"knob_path": "/tmp"})
    assert write_experiment_protocol(tc).startswith("ERROR:")


# write_next_experiment: sequential single-experiment designer

def test_write_next_from_object_input():
    with tempfile.TemporaryDirectory() as tmpdir:
        prop = ExperimentProposal(
            objective="o", name="screen_1", phase="Screen",
            levels=[ProposedLevel(knob="work_mem", value="64MB")],
            rationale="mover sweep",
        )
        tc = MockToolContext({"knob_path": tmpdir, "experiment_design_output": prop})
        result = write_next_experiment(tc)
        assert "OK: wrote experiment 'screen_1' (screen, 1 knob(s))" in result
        stored = tc.state["next_experiment"]
        assert stored["name"] == "screen_1"
        assert stored["phase"] == "screen"
        assert stored["objective"] == "o"
        assert stored["rationale"] == "mover sweep"
        assert stored["levels"][0]["knob"] == "work_mem"
        out_file = os.path.join(tmpdir, "experiment-protocol.json")
        assert os.path.isfile(out_file)
        with open(out_file) as f:
            assert json.load(f)["name"] == "screen_1"


def test_write_next_from_dict_and_wrapper():
    with tempfile.TemporaryDirectory() as tmpdir:
        tc = MockToolContext({
            "knob_path": tmpdir,
            "experiment_design_output": {
                "objective": "o", "name": "r1", "phase": "REFINEMENT",
                "levels": [{"name": "checkpoint_completion_target",
                            "recommended_value": "0.9"}],
                "rationale": "fine grid",
            },
        })
        result = write_next_experiment(tc)
        assert "OK: wrote experiment 'r1' (refinement, 1 knob(s))" in result
        assert tc.state["next_experiment"]["levels"][0]["knob"] == \
            "checkpoint_completion_target"

    with tempfile.TemporaryDirectory() as tmpdir:
        tc = MockToolContext({
            "knob_path": tmpdir,
            "experiment_design_output": {"experiment": {
                "objective": "o", "name": "i1", "phase": "interaction",
                "levels": [{"knob": "max_wal_size", "value": "4GB"}],
                "rationale": "coupling",
            }},
        })
        result = write_next_experiment(tc)
        assert "OK: wrote experiment 'i1' (interaction, 1 knob(s))" in result
        assert tc.state["next_experiment"]["name"] == "i1"


def test_write_next_bad_phase_writes_nothing():
    with tempfile.TemporaryDirectory() as tmpdir:
        tc = MockToolContext({
            "knob_path": tmpdir,
            "experiment_design_output": {
                "name": "bad1", "phase": "bogus-phase",
                "levels": [{"knob": "work_mem", "value": "64MB"}],
            },
        })
        result = write_next_experiment(tc)
        assert result.startswith("ERROR:")
        assert "bogus-phase" in result
        assert "next_experiment" not in tc.state
        assert not os.path.isfile(os.path.join(tmpdir, "experiment-protocol.json"))


def test_write_next_clamps_memory():
    with tempfile.TemporaryDirectory() as tmpdir:
        tc = MockToolContext({
            "knob_path": tmpdir,
            "memory_gb": 2.0,  # 40% cap on shared_buffers
            "experiment_design_output": {
                "name": "s1", "phase": "screen",
                "levels": [{"knob": "shared_buffers", "value": "100GB"}],
            },
        })
        result = write_next_experiment(tc)
        assert "OK: wrote experiment 's1'" in result
        assert tc.state["next_experiment"]["levels"][0]["value"] != "100GB"
        with open(os.path.join(tmpdir, "experiment-protocol.json")) as f:
            assert json.load(f)["levels"][0]["value"] != "100GB"


def test_write_next_missing_design():
    tc = MockToolContext({"knob_path": "/tmp"})
    assert write_next_experiment(tc).startswith("ERROR:")


# Prompt content: single-experiment framing

def test_prompt_single_experiment_framing():
    from src.knob_tuner.sub_agents.knob_recommender.prompt import (
        KNOB_RECOMMENDER_PROMPT as p,
    )
    pl = p.lower()
    assert "single next experiment" in pl  # single-experiment framing
    assert "experiment history" in pl  # history reading guidance
    assert "never repeat" in pl  # no identical-set repeats
    assert "screen" in pl and "interaction" in pl and "refinement" in pl
    assert "broad multi-knob sweep" in pl  # screen semantics
    assert "joint variation" in pl  # interaction semantics
    assert "tight grid" in pl  # refinement semantics
    assert "20 distinct" in pl  # screen floor
    assert "read_knob_details" in p  # must fetch before levels
    assert "Few-Shot Example" in p  # one compact few-shot single-experiment JSON
    assert "single-experiment" in pl  # few-shot marker
    assert "write_next_experiment" in p  # tool call
    assert "ExperimentProposal" in p  # structured return
    assert "characterize" in pl and "verify" in pl  # CoT steps
    for item in ("Phase validity", "Cap", "No-op ban"):
        assert item in p  # checklist items
    assert len(p.splitlines()) < 100  # stays tight


# Agent factory

def test_agent_is_single_experiment_designer():
    agent = create_knob_recommender_agent()
    assert agent.name == "knob_recommender"
    assert agent.output_key == "experiment_design_output"
    assert agent.output_schema == ExperimentProposal
    assert agent.generate_content_config.temperature == 0.0
    tool_names = [t.__name__ for t in agent.tools]
    assert "write_next_experiment" in tool_names
    assert "write_experiment_protocol" in tool_names
    assert "write_selected_knobs" in tool_names  # kept for compat
