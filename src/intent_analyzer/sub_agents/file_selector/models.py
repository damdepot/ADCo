"""Pydantic output schema for the file selector agent."""
from pydantic import BaseModel, Field


class FileSelectorOutput(BaseModel):
    files: list[str] = Field(
        description="Relative paths to files relevant to database interaction",
    )
    entry_point: str = Field(
        description="Relative path to the main application entry point",
    )