"""Alias/forwarder for migrated file_selector subagent."""
from src.intent_analyzer.sub_agents.file_selector.agent import create_file_selector_agent
from src.intent_analyzer.sub_agents.file_selector.models import FileSelectorOutput

__all__ = ["create_file_selector_agent", "FileSelectorOutput"]
