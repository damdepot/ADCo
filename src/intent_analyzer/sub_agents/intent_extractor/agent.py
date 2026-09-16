"""Intent extractor LlmAgent — extracts DB interaction patterns, optimization targets, and workload."""
from google.adk.agents import LlmAgent
from google.genai import types
from src.intent_analyzer.sub_agents.intent_extractor import prompt, tools
from src.intent_analyzer.sub_agents.intent_extractor.models import IntentExtractorOutput


def create_intent_extractor_agent(model: str = "gemini-3.5-flash-lite") -> LlmAgent:
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
    )