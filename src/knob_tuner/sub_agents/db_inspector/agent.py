"""db_inspector sub-agent — inspects live DB schema, parameters, and synthesizes with workload."""

from google.adk.agents import LlmAgent
from google.genai import types

from src.knob_tuner.sub_agents.db_inspector import prompt, tools
from src.knob_tuner.sub_agents.db_inspector.models import DbInspectorOutput


def create_db_inspector_agent(model: str = "gemini-3.5-flash-lite") -> LlmAgent:
    """Create and return the db_inspector LlmAgent."""
    return LlmAgent(
        name="db_inspector",
        model=model,
        instruction=prompt.DB_INSPECTOR_PROMPT,
        description="Inspects live database schema and parameters, saving knobs context for recommendation.",
        tools=[
            tools.check_schema,
            tools.extract_knobs,
            tools.write_knobs_file,
        ],
        output_schema=DbInspectorOutput,
        output_key="db_inspector_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
        ),
    )


# Backward compatibility alias
create_intent_analyzer_agent = create_db_inspector_agent
