"""Tests for ADK prompt template rendering across all code_rewriter sub-agents."""

import asyncio
from unittest.mock import MagicMock
import pytest

from google.adk.utils.instructions_utils import inject_session_state
from src.code_rewriter.agent import ROOT_PROMPT
from src.code_rewriter.sub_agents.code_optimizer.prompt import CODE_OPTIMIZER_AGENT_PROMPT
from src.code_rewriter.sub_agents.file_selector.prompt import FILE_SELECTOR_PROMPT
from src.code_rewriter.sub_agents.intent_extractor.prompt import INTENT_EXTRACTOR_PROMPT
from src.code_rewriter.sub_agents.verifier.prompt import VERIFIER_PROMPT


def _make_mock_readonly_context(state=None):
    ctx = MagicMock()
    ctx._invocation_context.session.state = state or {}
    ctx._invocation_context.artifact_service = None
    return ctx


@pytest.mark.parametrize(
    "name,prompt_text",
    [
        ("ROOT_PROMPT", ROOT_PROMPT),
        ("CODE_OPTIMIZER_AGENT_PROMPT", CODE_OPTIMIZER_AGENT_PROMPT),
        ("FILE_SELECTOR_PROMPT", FILE_SELECTOR_PROMPT),
        ("INTENT_EXTRACTOR_PROMPT", INTENT_EXTRACTOR_PROMPT),
        ("VERIFIER_PROMPT", VERIFIER_PROMPT),
    ],
)
def test_prompts_render_without_adk_template_variable_errors(name, prompt_text):
    """Ensure no unescaped curly braces in prompt templates trigger KeyError or break ADK rendering."""
    readonly_context = _make_mock_readonly_context()
    rendered = asyncio.run(inject_session_state(prompt_text, readonly_context))
    assert isinstance(rendered, str)
    assert len(rendered) > 0
