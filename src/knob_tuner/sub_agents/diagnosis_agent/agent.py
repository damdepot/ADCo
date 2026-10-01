"""Diagnosis LlmAgent — strategy-only correction for failed proposals, zero side effects."""

import asyncio

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types

from src.knob_tuner.stages.models import DiagnosisOutput
from src.knob_tuner.sub_agents.diagnosis_agent import prompt, tools


def make_buffer_callback(buffer_time: float = 0.0):
    if buffer_time <= 0:
        return None
    async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
        await asyncio.sleep(buffer_time)
    return buffer_callback


def create_diagnosis_agent(model: str | BaseLlm = "gemini-3.5-flash-lite", buffer_time: float = 0.0) -> LlmAgent:
    """Create and return the diagnosis LlmAgent (pure: read-only tools only)."""
    return LlmAgent(
        name="diagnosis_agent",
        model=model,
        instruction=prompt.DIAGNOSIS_AGENT_PROMPT,
        description="Diagnoses failed benchmark proposals into a strategy-only correction; never sets knob values or verdicts.",
        before_model_callback=make_buffer_callback(buffer_time),
        tools=[
            tools.read_knob_details,
        ],
        output_schema=DiagnosisOutput,
        output_key="diagnosis_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0,
        ),
    )
