"""Root agent entry point for the ADCo rewriter pipeline.

The orchestrator is now a deterministic ADK ``Workflow`` (see
``src.code_rewriter.workflow``). ``create_root_agent`` is kept as the public
entry point and returns that workflow.
"""

from typing import Union

from google.adk.models import BaseLlm

from src.code_rewriter.workflow import create_rewriter_workflow

__all__ = ["create_root_agent"]


def create_root_agent(
    model: Union[str, BaseLlm] = "gemini-3.5-flash-lite",
    buffer_time: float = 0.0,
):
    """Return the ADCo rewriter workflow as the root agent."""
    return create_rewriter_workflow(model, buffer_time=buffer_time)
