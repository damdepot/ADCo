"""Knob checker LlmAgent — validates recommended knobs in staging environment."""

import asyncio
from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types

from src.knob_tuner.sub_agents.knob_checker import prompt, tools
from src.knob_tuner.sub_agents.knob_checker.models import KnobCheckerOutput


def make_buffer_callback(buffer_time: float = 0.0):
    if buffer_time <= 0:
        return None
    async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
        await asyncio.sleep(buffer_time)
    return buffer_callback


def create_knob_checker_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite", buffer_time: float = 0.0) -> LlmAgent:
    """Create and return the knob checker LlmAgent."""
    return LlmAgent(
        name="knob_checker",
        model=model,
        instruction=prompt.KNOB_CHECKER_PROMPT,
        description="Validates database knob recommendations in the staging environment by measuring baseline performance, applying parameters, restarting, executing health and CRUD tests, and verifying tuned stress performance.",
        before_model_callback=make_buffer_callback(buffer_time),
        tools=[
            tools.setup_staging_docker,
            tools.benchmark_baseline_staging,
            tools.apply_knobs_staging,
            tools.restart_database_staging,
            tools.recreate_database_staging,
            tools.test_database_staging,
            tools.benchmark_tuned_staging,
            tools.cleanup_staging_docker,
        ],
        output_schema=KnobCheckerOutput,
        output_key="knob_checker_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0,
        ),
    )
