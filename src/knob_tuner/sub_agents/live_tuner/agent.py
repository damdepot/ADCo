"""Live tuner LlmAgent — safely applies validated dynamic knobs to production."""

from google.adk.agents import LlmAgent
from google.genai import types

from src.knob_tuner.sub_agents.live_tuner import prompt, tools
from src.knob_tuner.sub_agents.live_tuner.models import LiveTunerOutput


def create_live_tuner_agent(model: str = "gemini-3.5-flash-lite") -> LlmAgent:
    """Create and return the live tuner LlmAgent."""
    return LlmAgent(
        name="live_tuner",
        model=model,
        instruction=prompt.LIVE_TUNER_PROMPT,
        description="Safely applies validated dynamic configuration knobs to the live production database while strictly avoiding automatic database restarts.",
        tools=[
            tools.check_staging_validation,
            tools.apply_knobs_production,
        ],
        output_schema=LiveTunerOutput,
        output_key="live_tuner_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
        ),
    )
