"""Intent extractor LlmAgent — extracts database interaction intent from code."""

from google.adk.agents import LlmAgent
from google.genai import types

from rewriter.sub_agents.intent_extractor import prompt, tools
from rewriter.sub_agents.intent_extractor.models import IntentExtractorOutput


def create_intent_extractor_agent(model: str = "gemini-2.5-flash") -> LlmAgent:
    return LlmAgent(
        name="intent_extractor",
        model=model,
        instruction=prompt.INTENT_EXTRACTOR_PROMPT,
        description="Extracts database interaction patterns and intent from code files.",
        tools=[tools.read_selected_files],
        output_schema=IntentExtractorOutput,
        output_key="intent_extractor_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
        ),
    )