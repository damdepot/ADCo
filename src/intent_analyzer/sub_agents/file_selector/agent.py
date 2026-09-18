"""File selector LlmAgent — picks files related to database interaction."""
import asyncio
from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types
from src.intent_analyzer.sub_agents.file_selector.models import FileSelectorOutput
from src.intent_analyzer.sub_agents.file_selector import prompt


def make_buffer_callback(buffer_time: float = 0.0):
    if buffer_time <= 0:
        return None
    async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
        await asyncio.sleep(buffer_time)
    return buffer_callback


def create_file_selector_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite", buffer_time: float = 0.0) -> LlmAgent:
    return LlmAgent(
        name="file_selector",
        model=model,
        instruction=prompt.FILE_SELECTOR_PROMPT,
        description="Selects files from a codebase that are relevant to database interaction.",
        output_schema=FileSelectorOutput,
        output_key="file_selector_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0,
        ),
        before_model_callback=make_buffer_callback(buffer_time),
    )