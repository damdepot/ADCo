"""Verifier LlmAgent — syntax-checks and runs sandbox application."""

import asyncio
from typing import Union

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.genai import types

from src.code_rewriter.sub_agents.verifier import prompt
from src.code_rewriter.sub_agents.verifier import tools
from src.code_rewriter.sub_agents.verifier.models import VerifierOutput


async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
    """Add a small buffer time before back-to-back LLM calls to prevent rate limiting."""
    await asyncio.sleep(3)


def create_verifier_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite") -> LlmAgent:
    return LlmAgent(
        name="verifier",
        model=model,
        before_model_callback=buffer_callback,
        output_key="verifier_output",
        output_schema=VerifierOutput,
        instruction=prompt.VERIFIER_PROMPT,
        description="Verifies generated code by comparing original vs modified, syntax-checking, and running the application in the sandbox. Provides optimization suggestions only when needed.",
        tools=[tools.compare_original_and_modified, tools.check_syntax, tools.run_application],
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
        ),
    )