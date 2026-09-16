"""Knob recommender LlmAgent — formulates DB configuration recommendations."""

from google.adk.agents import LlmAgent
from google.genai import types

from src.knob_tuner.sub_agents.knob_recommender import prompt, tools
from src.knob_tuner.sub_agents.knob_recommender.models import KnobRecommenderOutput


def create_knob_recommender_agent(model: str = "gemini-3.5-flash-lite") -> LlmAgent:
    """Create and return the knob recommender LlmAgent."""
    return LlmAgent(
        name="knob_recommender",
        model=model,
        instruction=prompt.KNOB_RECOMMENDER_PROMPT,
        description="Recommends optimal database configuration knobs based on workload patterns, hardware limits, and DBA best practices.",
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
