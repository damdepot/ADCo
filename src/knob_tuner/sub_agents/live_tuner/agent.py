"""Live tuner LlmAgent — safely applies validated dynamic knobs to production."""

import asyncio
from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types

from src.knob_tuner.sub_agents.live_tuner import prompt, tools
from src.knob_tuner.sub_agents.live_tuner.models import LiveTunerOutput


def make_buffer_callback(buffer_time: float = 0.0):
    if buffer_time <= 0:
        return None
    async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
        await asyncio.sleep(buffer_time)
    return buffer_callback


def create_live_tuner_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite", buffer_time: float = 0.0) -> LlmAgent:
    """Create and return the live tuner LlmAgent."""
    return LlmAgent(
        name="live_tuner",
        model=model,
        instruction=prompt.LIVE_TUNER_PROMPT,
        description="Safely applies validated dynamic configuration knobs to the live production database while strictly avoiding automatic database restarts.",
        before_model_callback=make_buffer_callback(buffer_time),
        tools=[
            tools.check_staging_validation,
            tools.apply_knobs_production,
        ],
        output_schema=LiveTunerOutput,
        output_key="live_tuner_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0,
        ),
    )
