"""Knob recommender LlmAgent — formulates DB configuration recommendations."""

import asyncio
from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types

from src.knob_tuner.sub_agents.knob_recommender import prompt, tools
from src.knob_tuner.sub_agents.knob_recommender.models import KnobRecommenderOutput


async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
    """Add a small buffer time before back-to-back LLM calls to prevent rate limiting."""
    await asyncio.sleep(3)


def create_knob_recommender_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite") -> LlmAgent:
    """Create and return the knob recommender LlmAgent."""
    return LlmAgent(
        name="knob_recommender",
        model=model,
        instruction=prompt.KNOB_RECOMMENDER_PROMPT,
        description="Recommends optimal database configuration knobs based on workload patterns, hardware limits, and DBA best practices.",
        before_model_callback=buffer_callback,
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
