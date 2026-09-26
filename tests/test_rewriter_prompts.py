"""Tests for ADK prompt template rendering across all code_rewriter sub-agents."""

import asyncio
from unittest.mock import MagicMock
import pytest

from google.adk.utils.instructions_utils import inject_session_state
from src.code_rewriter.sub_agents.optimizer.prompt import OPTIMIZER_AGENT_PROMPT
from src.intent_analyzer.sub_agents.file_selector.prompt import FILE_SELECTOR_PROMPT
from src.intent_analyzer.sub_agents.intent_extractor.prompt import INTENT_EXTRACTOR_PROMPT
from src.code_rewriter.sub_agents.verifier.prompt import VERIFIER_PROMPT


def _make_mock_readonly_context(state=None):
    ctx = MagicMock()
    ctx._invocation_context.session.state = state or {}
    ctx._invocation_context.artifact_service = None
    return ctx


@pytest.mark.parametrize(
    "name,prompt_text",
    [
        ("OPTIMIZER_AGENT_PROMPT", OPTIMIZER_AGENT_PROMPT),
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


def test_optimizer_prompt_has_repair_mode_section():
    """Ensure the optimizer prompt documents repair-mode behavior."""
    assert "## Repair mode" in OPTIMIZER_AGENT_PROMPT
    text = OPTIMIZER_AGENT_PROMPT.lower()
    assert "repair request" in text
    assert "your previous attempt" in text
    assert "diff vs original" in text
    assert "full function" in text


def test_optimizer_prompt_has_sql_safety_rules():
    """Ensure the optimizer prompt documents the runtime-crash-prevention SQL rules."""
    text = OPTIMIZER_AGENT_PROMPT.lower()
    assert "exactly one sql statement" in text
    assert "never join multiple statements" in text
    assert "never concatenate" in text
    assert "`where` clause" in text
    assert "placeholders" in text
    assert "number of parameter values" in text


def test_optimizer_prompt_steers_to_composite_key_batching():
    """Ensure the optimizer prompt forbids per-group query loops and names the composite-key form."""
    text = OPTIMIZER_AGENT_PROMPT.lower()
    assert "composite-key" in text
    assert "one query per group" in text


def test_verifier_prompt_requires_evidence():
    """Ensure the verifier prompt gates findings on verbatim evidence."""
    text = VERIFIER_PROMPT.lower()
    assert "evidence" in text
    assert "current_contract" in VERIFIER_PROMPT
