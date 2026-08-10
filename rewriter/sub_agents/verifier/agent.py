"""Verifier LlmAgent — syntax-checks and runs sandbox application."""

from google.adk.agents import LlmAgent
from google.genai import types

from rewriter.sub_agents.verifier import prompt, tools
from rewriter.sub_agents.verifier.models import VerifierOutput


def create_verifier_agent(model: str = "gemini-2.5-flash") -> LlmAgent:
    return LlmAgent(
        name="verifier",
        model=model,
        output_key="verifier_output",
        output_schema=VerifierOutput,
        instruction=prompt.VERIFIER_PROMPT,
        description="Verifies generated code by comparing original vs modified, syntax-checking, and running the application in the sandbox. Provides optimization suggestions only when needed.",
        tools=[tools.compare_original_and_modified, tools.check_syntax, tools.run_application],
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
        ),
    )