"""Code optimizer LlmAgent — applies rewrite strategies to optimize code using file tools."""

from typing import Union
from google.adk.models import BaseLlm

from google.adk.agents import LlmAgent
from google.genai import types

from src.code_rewriter._common import make_buffer_callback
from src.code_rewriter.sub_agents.code_optimizer import tools
from src.code_rewriter.sub_agents.code_optimizer import prompt
from src.code_rewriter.sub_agents.code_optimizer.models import CodeOptimizerOutput


def create_code_optimizer_agent(model: Union[str, BaseLlm] = "gemini-3.5-flash-lite", buffer_time: float = 0.0) -> LlmAgent:
    # ponytail: one orchestrator attempt = one model call; no node-level RetryConfig
    # so max_attempts=3 does not stack with the root 5-attempt budget / HTTP retries.
    return LlmAgent(
        name="code_optimizer",
        model=model,
        before_model_callback=make_buffer_callback(buffer_time),
        instruction=prompt.CODE_OPTIMIZER_AGENT_PROMPT,
        description="Optimizes database interaction code using rewrite strategies. Reads files from sandbox, writes optimized versions.",
        tools=[tools.read_file, tools.write_file, tools.replace_function, tools.list_sandbox, tools.get_optimization_context],
        output_key="code_optimizer_output",
        output_schema=CodeOptimizerOutput,
        generate_content_config=types.GenerateContentConfig(
            max_output_tokens=65536,
            temperature=0.0,
        ),
    )