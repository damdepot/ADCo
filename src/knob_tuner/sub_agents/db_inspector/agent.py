"""db_inspector sub-agent — inspects live DB schema, parameters, and synthesizes with workload."""

import asyncio
from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types

from src.knob_tuner.sub_agents.db_inspector import prompt, tools
from src.knob_tuner.sub_agents.db_inspector.models import DbInspectorOutput


def make_buffer_callback(buffer_time: float = 0.0):
    if buffer_time <= 0:
        return None
    async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
        await asyncio.sleep(buffer_time)
    return buffer_callback


def create_db_inspector_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite", buffer_time: float = 0.0) -> LlmAgent:
    """Create and return the db_inspector LlmAgent."""
    return LlmAgent(
        name="db_inspector",
        model=model,
        instruction=prompt.DB_INSPECTOR_PROMPT,
        description="Inspects live database schema and parameters, saving knobs context for recommendation.",
        before_model_callback=make_buffer_callback(buffer_time),
        tools=[
            tools.check_schema,
            tools.extract_knobs,
            tools.write_knobs_file,
        ],
        output_schema=DbInspectorOutput,
        output_key="db_inspector_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0,
        ),
    )
