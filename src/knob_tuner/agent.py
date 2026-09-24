"""Root agent entry point for the ADCo knob_tuner pipeline.

The orchestrator is now a deterministic ADK ``Workflow`` (see
``src.knob_tuner.workflow``). ``create_root_agent`` is kept as the public entry
point and returns that workflow.
"""

from typing import Union

from google.adk.models import BaseLlm

from src.knob_tuner.workflow import create_knob_tuner_workflow

__all__ = ["create_root_agent"]


def create_root_agent(
    model: Union[str, BaseLlm] = "gemini-3.5-flash-lite",
    buffer_time: float = 0.0,
):
    """Return the ADCo knob_tuner workflow as the root agent."""
    return create_knob_tuner_workflow(model, buffer_time=buffer_time)
