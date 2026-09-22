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
    function: str = Field(
        default="",
        description="Qualified name of the target function optimized",
    )
    file: str = Field(
        default="",
        description="Relative path of the modified file",
    )
    status: str = Field(
        default="",
        description="PASS if the target function was optimized, FAIL otherwise",
    )