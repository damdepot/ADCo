"""Wave 2a purity tests: knob-tuner agents are pure (structured outputs only, zero side effects)."""

from src.knob_tuner.stages.models import CandidateProposal, DiagnosisOutput
from src.knob_tuner.sub_agents.candidate_generator import (
    agent as gen_agent_mod,
)
from src.knob_tuner.sub_agents.candidate_generator import (
    prompt as gen_prompt_mod,
)
from src.knob_tuner.sub_agents.db_inspector import agent as insp_agent_mod
from src.knob_tuner.sub_agents.db_inspector import prompt as insp_prompt_mod
from src.knob_tuner.sub_agents.db_inspector.models import DbInspectorOutput
from src.knob_tuner.sub_agents.diagnosis_agent import agent as diag_agent_mod
from src.knob_tuner.sub_agents.diagnosis_agent import prompt as diag_prompt_mod

_FORBIDDEN_TOOL_SUBSTRINGS = ("write", "apply", "restart")
_FORBIDDEN_PROMPT_STRINGS = (
    "write_selected_knobs",
    "write_next_experiment",
    "write_experiment_protocol",
    "write_knobs_file",
)


def _tool_names(agent) -> list[str]:
    names = []
    for tool in agent.tools or []:
        names.append(
            getattr(tool, "name", None)
            or getattr(tool, "__name__", None)
            or type(tool).__name__
        )
    return names


def test_db_inspector_tools_have_no_side_effects():
    agent = insp_agent_mod.create_db_inspector_agent()
    names = _tool_names(agent)
    assert names, "db_inspector must expose read tools"
    for name in names:
        lowered = name.lower()
        for banned in _FORBIDDEN_TOOL_SUBSTRINGS:
            assert banned not in lowered, f"db_inspector tool {name!r} looks stateful"


def test_candidate_generation_tools_have_no_side_effects():
    agent = gen_agent_mod.create_candidate_generation_agent()
    names = _tool_names(agent)
    assert names, "candidate_generation_agent must expose read tools"
    for name in names:
        lowered = name.lower()
        for banned in _FORBIDDEN_TOOL_SUBSTRINGS:
            assert banned not in lowered, f"candidate tool {name!r} looks stateful"


def test_candidate_generation_tools_are_read_only_pair():
    agent = gen_agent_mod.create_candidate_generation_agent()
    assert set(_tool_names(agent)) == {"get_knob_strategies", "read_knob_details"}


def test_agents_have_output_schema_and_key():
    inspector = insp_agent_mod.create_db_inspector_agent()
    assert inspector.output_schema is DbInspectorOutput
    assert inspector.output_key == "db_inspector_output"

    generator = gen_agent_mod.create_candidate_generation_agent()
    assert generator.output_schema is CandidateProposal
    assert generator.output_key == "candidate_proposal"

    diagnosis = diag_agent_mod.create_diagnosis_agent()
    assert diagnosis.output_schema is DiagnosisOutput
    assert diagnosis.output_key == "diagnosis_output"


def test_agent_temperatures():
    inspector = insp_agent_mod.create_db_inspector_agent()
    generator = gen_agent_mod.create_candidate_generation_agent()
    diagnosis = diag_agent_mod.create_diagnosis_agent()
    assert inspector.generate_content_config.temperature == 0.0
    assert diagnosis.generate_content_config.temperature == 0.0
    assert generator.generate_content_config.temperature != 0.0


def test_prompts_contain_no_write_tool_references():
    for prompt_text in (
        insp_prompt_mod.DB_INSPECTOR_PROMPT,
        gen_prompt_mod.CANDIDATE_GENERATOR_PROMPT,
        diag_prompt_mod.DIAGNOSIS_AGENT_PROMPT,
    ):
        assert prompt_text.strip(), "prompt must not be empty"
        for banned in _FORBIDDEN_PROMPT_STRINGS:
            assert banned not in prompt_text, f"prompt references {banned!r}"


def test_candidate_prompt_keeps_campaign_guidance_with_20_cap():
    # Unified prompt: the compounding campaign directive stays (EXPLOIT+EXPLORE
    # seeds from the confirmed building blocks at 2-4 knobs) under the hard
    # 20-knob cap.
    text = gen_prompt_mod.CANDIDATE_GENERATOR_PROMPT
    assert "at most 20 distinct knobs" in text
    assert "1-4 knobs" not in text
    assert "campaign directive" in text
    assert "2-4 knobs" in text
