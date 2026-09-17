"""Live tuner LlmAgent — safely applies validated dynamic knobs to production."""

import asyncio
from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types

from src.knob_tuner.sub_agents.live_tuner import prompt, tools
from src.knob_tuner.sub_agents.live_tuner.models import LiveTunerOutput


async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
    """Add a small buffer time before back-to-back LLM calls to prevent rate limiting."""
    await asyncio.sleep(3)


def create_live_tuner_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite") -> LlmAgent:
    """Create and return the live tuner LlmAgent."""
    return LlmAgent(
        name="live_tuner",
        model=model,
        instruction=prompt.LIVE_TUNER_PROMPT,
        description="Safely applies validated dynamic configuration knobs to the live production database while strictly avoiding automatic database restarts.",
        before_model_callback=buffer_callback,
        tools=[
            tools.check_staging_validation,
            tools.apply_knobs_production,
        ],
        output_schema=LiveTunerOutput,
        output_key="live_tuner_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
        ),
    )
