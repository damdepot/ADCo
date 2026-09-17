"""Intent extractor LlmAgent — extracts DB interaction patterns, optimization targets, and workload."""
import asyncio
from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types
from src.intent_analyzer.sub_agents.intent_extractor import prompt, tools
from src.intent_analyzer.sub_agents.intent_extractor.models import IntentExtractorOutput


async def buffer_callback(ctx, req):
    await asyncio.sleep(3)


def create_intent_extractor_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite") -> LlmAgent:
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
        before_model_callback=buffer_callback,
    )