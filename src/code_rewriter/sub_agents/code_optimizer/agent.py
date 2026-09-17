"""Code optimizer LlmAgent — applies rewrite strategies to optimize code using file tools."""

import asyncio
from typing import Union
from google.adk.models import BaseLlm

from google.adk.agents import LlmAgent
from google.adk.workflow._retry_config import RetryConfig
from google.genai import types

from src.code_rewriter.sub_agents.code_optimizer import tools
from src.code_rewriter.sub_agents.code_optimizer import prompt
from src.code_rewriter.sub_agents.code_optimizer.models import CodeOptimizerOutput


_CODE_OPTIMIZER_RETRY_CONFIG = RetryConfig(
    max_attempts=3,
    initial_delay=1.0,
    max_delay=10.0,
    backoff_factor=2.0,
    jitter=0.1,
)


def make_buffer_callback(buffer_time: float = 0.0):
    if buffer_time <= 0:
        return None
    async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
        await asyncio.sleep(buffer_time)
    return buffer_callback


def create_code_optimizer_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite", buffer_time: float = 0.0) -> LlmAgent:
    return LlmAgent(
        name="code_optimizer",
        model=model,
        before_model_callback=make_buffer_callback(buffer_time),
        instruction=prompt.CODE_OPTIMIZER_AGENT_PROMPT,
        description="Optimizes database interaction code using rewrite strategies. Reads files from sandbox, writes optimized versions.",
        tools=[tools.read_file, tools.write_file, tools.list_sandbox, tools.get_optimization_context],
        output_key="code_optimizer_output",
        output_schema=CodeOptimizerOutput,
        retry_config=_CODE_OPTIMIZER_RETRY_CONFIG,
        generate_content_config=types.GenerateContentConfig(
            max_output_tokens=65536,
            temperature=0.1,
        ),
    )