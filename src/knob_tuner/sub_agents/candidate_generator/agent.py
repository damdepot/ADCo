"""Candidate generation LlmAgent — proposes one knob set per call, zero side effects."""

import asyncio

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types

from src.knob_tuner.stages.models import CandidateProposal
from src.knob_tuner.sub_agents.candidate_generator import prompt, tools


def make_buffer_callback(buffer_time: float = 0.0):
    if buffer_time <= 0:
        return None
    async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
        await asyncio.sleep(buffer_time)
    return buffer_callback


def create_candidate_generation_agent(model: str | BaseLlm = "gemini-3.5-flash-lite", buffer_time: float = 0.0) -> LlmAgent:
    """Create and return the candidate_generation LlmAgent (pure: read-only tools only)."""
    return LlmAgent(
        name="candidate_generation_agent",
        model=model,
        instruction=prompt.CANDIDATE_GENERATOR_PROMPT,
        description="Proposes a single database configuration experiment per call using read-only knob references; never persists.",
        before_model_callback=make_buffer_callback(buffer_time),
        tools=[
            tools.get_knob_strategies,
            tools.read_knob_details,
        ],
        output_schema=CandidateProposal,
        output_key="candidate_proposal",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.2,
        ),
    )
