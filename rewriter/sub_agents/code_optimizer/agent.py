"""Code optimizer LlmAgent — applies rewrite strategies to optimize code using file tools."""

from google.adk.agents import LlmAgent
from google.adk.workflow._retry_config import RetryConfig
from google.genai import types

from rewriter.sub_agents.code_optimizer import prompt, tools
from rewriter.sub_agents.code_optimizer.models import CodeOptimizerOutput


_CODE_OPTIMIZER_RETRY_CONFIG = RetryConfig(
    max_attempts=3,
    initial_delay=1.0,
    max_delay=10.0,
    backoff_factor=2.0,
    jitter=0.1,
)


def create_code_optimizer_agent(model: str = "gemini-2.5-flash") -> LlmAgent:
    return LlmAgent(
        name="code_optimizer",
        model=model,
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