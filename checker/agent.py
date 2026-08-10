"""Checker LlmAgent — single agent that audits sandbox-optimized code for safety issues."""

from google.adk.agents import LlmAgent
from google.genai import types

from checker import prompt, tools
from checker.models import CheckerOutput


def create_checker_agent(model: str = "gemini-2.5-flash") -> LlmAgent:
    return LlmAgent(
        name="adco_checker",
        model=model,
        instruction=prompt.CHECKER_PROMPT,
        description="Safety checker — detects correctness, safety, regression, completeness, and performance issues in sandbox-optimized code before production.",
        tools=[
            tools.find_modified_files,
            tools.read_file,
            tools.read_original_file,
            tools.list_sandbox,
        ],
        output_key="checker_output",
        output_schema=CheckerOutput,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
        ),
    )
