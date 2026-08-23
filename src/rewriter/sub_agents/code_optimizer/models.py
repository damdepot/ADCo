"""Pydantic output schema for the code optimizer agent."""
from pydantic import BaseModel, Field


class CodeOptimizerOutput(BaseModel):
    modified_files: list[str] = Field(
        default_factory=list,
        description="List of relative file paths that were modified",
    )
    summary: str = Field(
        default="",
        description="Summary of the optimizations applied",
    )