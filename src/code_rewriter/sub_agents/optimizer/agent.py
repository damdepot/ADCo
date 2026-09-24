"""Optimizer LlmAgent — applies rewrite strategies to optimize a target function."""

from typing import Union
from google.adk.models import BaseLlm

from google.adk.agents import LlmAgent
from google.genai import types

from src.code_rewriter._common import make_buffer_callback
from src.code_rewriter.sub_agents.optimizer import tools
from src.code_rewriter.sub_agents.optimizer import prompt
from src.code_rewriter.sub_agents.optimizer.models import OptimizerOutput


def create_optimizer_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite", buffer_time: float = 0.0) -> LlmAgent:
    # ponytail: one orchestrator attempt = one model call; no node-level RetryConfig,
    # HTTP retries live on the model in main.py.
    return LlmAgent(
        name="optimizer",
        model=model,
        before_model_callback=make_buffer_callback(buffer_time),
        instruction=prompt.OPTIMIZER_AGENT_PROMPT,
        description="Optimizes one target function's database interaction code using the optimization context and surgically replaces it.",
        tools=[tools.get_optimization_context, tools.replace_function],
        output_key="optimizer_output",
        output_schema=OptimizerOutput,
        generate_content_config=types.GenerateContentConfig(
            max_output_tokens=65536,
            temperature=0.0,
        ),
    )
