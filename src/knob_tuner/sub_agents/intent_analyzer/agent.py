"""Intent analyzer LlmAgent — analyzes DB schema, knobs, and workload patterns."""

from google.adk.agents import LlmAgent
from google.genai import types

from src.knob_tuner.sub_agents.intent_analyzer import prompt, tools
from src.knob_tuner.sub_agents.intent_analyzer.models import IntentAnalyzerOutput


def create_intent_analyzer_agent(model: str = "gemini-3.5-flash-lite") -> LlmAgent:
    """Create and return the intent analyzer LlmAgent."""
    return LlmAgent(
        name="intent_analyzer",
        model=model,
        instruction=prompt.INTENT_ANALYZER_PROMPT,
        description="Analyzes database schema, configuration knobs, hardware capacity, and workload patterns.",
        tools=[
            tools.check_schema,
            tools.extract_knobs,
            tools.scan_codebase_workload,
            tools.write_knobs_file,
        ],
        output_schema=IntentAnalyzerOutput,
        output_key="intent_analyzer_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
        ),
    )
