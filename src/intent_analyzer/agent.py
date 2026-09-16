"""Root orchestrator agent for the intent analyzer module."""
from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool

from src.intent_analyzer.sub_agents.file_selector.agent import create_file_selector_agent
from src.intent_analyzer.sub_agents.intent_extractor.agent import create_intent_extractor_agent
from src.intent_analyzer.tools.scanner import scan_codebase

INTENT_ANALYZER_PROMPT = """You are the ADCo Codebase Intent Analyzer. Your mission is to scan the
target application codebase, select database-relevant files, and extract both concrete code optimization
targets and structured database workload characteristics.

## Pipeline Steps

1. `scan_codebase` — Call this tool first to scan the directory structure of the target codebase.
   The tool returns the file listing and stores it in session state.

2. `file_selector` (sub-agent) — Delegate to it, providing the file listing from step 1.
   It identifies the database-relevant files and the main application entry point.

3. `intent_extractor` (sub-agent) — Delegate to it.
   It reads the selected files and extracts:
   - Code-level DB interaction patterns & concrete optimization targets
   - Structured workload characteristics (query types, ORM, transactions, read/write ratio, notable patterns)

## Rules
- Call exactly ONE tool per turn, then wait.
- Always execute step 1 -> step 2 -> step 3 in order.
- Provide clear summary of the findings upon completion.
"""


def create_intent_analyzer_agent(model: str = "gemini-3.5-flash-lite") -> LlmAgent:
    """Create and return the root intent analyzer LlmAgent."""
    return LlmAgent(
        name="intent_analyzer",
        model=model,
        instruction=INTENT_ANALYZER_PROMPT,
        description="ADCo codebase intent analyzer — scans codebase, selects DB files, and extracts optimization targets and workload profile.",
        tools=[
            scan_codebase,
            AgentTool(create_file_selector_agent(model)),
            AgentTool(create_intent_extractor_agent(model)),
        ],
    )


def create_root_agent(model: str = "gemini-3.5-flash-lite") -> LlmAgent:
    """Alias for create_intent_analyzer_agent for consistency."""
    return create_intent_analyzer_agent(model)
