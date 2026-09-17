"""Knob recommender LlmAgent — formulates DB configuration recommendations."""

import asyncio
from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types

from src.knob_tuner.sub_agents.knob_recommender import prompt, tools
from src.knob_tuner.sub_agents.knob_recommender.models import KnobRecommenderOutput


def make_buffer_callback(buffer_time: float = 0.0):
    if buffer_time <= 0:
        return None
    async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
        await asyncio.sleep(buffer_time)
    return buffer_callback


def create_knob_recommender_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite", buffer_time: float = 0.0) -> LlmAgent:
    """Create and return the knob recommender LlmAgent."""
    return LlmAgent(
        name="knob_recommender",
        model=model,
        instruction=prompt.KNOB_RECOMMENDER_PROMPT,
        description="Recommends optimal database configuration knobs based on workload patterns, hardware limits, and DBA best practices.",
        before_model_callback=make_buffer_callback(buffer_time),
        tools=[
            tools.get_knob_strategies,
            tools.read_knobs_file,
            tools.write_selected_knobs,
        ],
        output_schema=KnobRecommenderOutput,
        output_key="knob_recommender_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
        ),
    )
