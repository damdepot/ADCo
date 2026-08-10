"""File selector LlmAgent — picks files related to database interaction."""
from google.adk.agents import LlmAgent
from google.genai import types
from rewriter.sub_agents.file_selector.models import FileSelectorOutput
from rewriter.sub_agents.file_selector import prompt


def create_file_selector_agent(model: str = "gemini-2.5-flash") -> LlmAgent:
    return LlmAgent(
        name="file_selector",
        model=model,
        instruction=prompt.FILE_SELECTOR_PROMPT,
        description="Selects files from a codebase that are relevant to database interaction.",
        output_schema=FileSelectorOutput,
        output_key="file_selector_output",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1,
        ),
    )