"""ADCo orchestrator agent — coordinates intent_analyzer, code_rewriter, and knob_tuner as sub-agents."""

from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool

from src.intent_analyzer.agent import create_intent_analyzer_agent
from src.code_rewriter.agent import create_root_agent as create_rewriter_agent
from src.knob_tuner.agent import create_root_agent as create_tuner_agent

ORCHESTRATOR_PROMPT = """You are the ADCo pipeline orchestrator. You coordinate three
specialized sub-pipelines to jointly analyze codebase intent, optimize application code,
and tune database configuration knobs.

## Phase 1 — Codebase Intent & Workload Analysis (always required)

Delegate to `intent_analyzer`:
  "Analyze the codebase at: {target}. Scan files, select DB-relevant files, and extract optimization targets and workload characteristics."

Wait for `intent_analyzer` to complete. It stores `intent_output` and `workload_info` in session state.

## Phase 2 — Code Rewriting (executed when mode is `all` or `rewrite-only`)

Delegate to `adco_rewriter`:
  "Optimize the database interaction code in the codebase at: {target}. The intent has already been extracted into state."

After it completes, check `verifier_output` in state:
- status == "PASS" → proceed to Phase 3.
- status == "FAIL" → STOP immediately. Report the failure category, reason,
  and detail. Do NOT proceed to knob tuning.

## Phase 3 — Knob Tuning (executed when mode is `all` or `tune-only`)

Only run this phase if Phase 2 was successful or skipped.

Delegate to `knob_tuner`:
  "Tune database configuration knobs for the codebase at: {sandbox} (if Phase 2 was run) or {target} (if Phase 2 was skipped).
   [full DB configuration details from initial request]"

## Rules
- Call exactly ONE tool per turn, then wait.
- Always execute in order: Phase 1 (intent_analyzer) -> Phase 2 (code_rewriter) -> Phase 3 (knob_tuner).
- On any Phase 2 failure, STOP immediately — never invoke `knob_tuner`.
- Skip phases as dictated by the requested mode.
"""


def create_orchestrator_agent(model: str = "gemini-3.5-flash") -> LlmAgent:
    return LlmAgent(
        name="adco_orchestrator",
        model=model,
        instruction=ORCHESTRATOR_PROMPT,
        description=(
            "ADCo pipeline orchestrator — runs intent_analyzer (Phase 1), "
            "code_rewriter (Phase 2), and optionally knob_tuner (Phase 3) as ADK sub-agents."
        ),
        tools=[
            AgentTool(create_intent_analyzer_agent(model)),
            AgentTool(create_rewriter_agent(model)),
            AgentTool(create_tuner_agent(model)),
        ],
    )
