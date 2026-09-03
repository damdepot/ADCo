"""Knob checker LlmAgent — validates recommended knobs in staging environment."""

from google.adk.agents import LlmAgent
from google.genai import types

from src.knob_tuner.sub_agents.knob_checker import prompt, tools
from src.knob_tuner.sub_agents.knob_checker.models import KnobCheckerOutput


def create_knob_checker_agent(model: str = "gemini-3.5-flash-lite") -> LlmAgent:
    """Create and return the knob checker LlmAgent."""
    return LlmAgent(
        name="knob_checker",
        model=model,
        instruction=prompt.KNOB_CHECKER_PROMPT,
        description="Validates database knob recommendations in the staging environment by measuring baseline performance, applying parameters, restarting, executing health and CRUD tests, and verifying tuned stress performance.",
        tools=[
            tools.benchmark_baseline_staging,
            tools.apply_knobs_staging,
            tools.restart_database_staging,
            tools.test_database_staging,
            tools.benchmark_tuned_staging,
        ],
        output_schema=KnobCheckerOutput,
        output_key="knob_checker_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
        ),
    )
