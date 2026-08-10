"""Pydantic output schema for the file selector agent."""
from pydantic import BaseModel, Field


class FileSelectorOutput(BaseModel):
    files: list[str] = Field(
        description="Relative paths of files relevant to database interaction",
    )
    entry_point: str = Field(
        description="Relative path to the application entry point file",
    )