"""Intent extractor LlmAgent — extracts DB interaction patterns, optimization targets, and workload."""
import asyncio
from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types
from src.intent_analyzer.sub_agents.intent_extractor import prompt, tools
from src.intent_analyzer.sub_agents.intent_extractor.models import IntentExtractorOutput


def make_buffer_callback(buffer_time: float = 0.0):
    if buffer_time <= 0:
        return None
    async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
        await asyncio.sleep(buffer_time)
    return buffer_callback


def create_intent_extractor_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite", buffer_time: float = 0.0) -> LlmAgent:
    return LlmAgent(
        name="intent_extractor",
        model=model,
        instruction=prompt.INTENT_EXTRACTOR_PROMPT,
        description="Extracts database interaction patterns, code optimization targets, and workload characteristics from source files.",
        tools=[tools.read_selected_files],
        output_schema=IntentExtractorOutput,
        output_key="intent_extractor_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
        ),
        before_model_callback=make_buffer_callback(buffer_time),
    )