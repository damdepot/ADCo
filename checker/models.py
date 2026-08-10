"""Pydantic output schemas for the checker agent."""

from typing import Literal
from pydantic import BaseModel, Field


class CheckerIssue(BaseModel):
    """A single issue found in optimized code."""
    file: str = Field(description="Relative file path in the sandbox")
    line: int = Field(default=0, description="Approximate line number (0 if unknown)")
    severity: Literal["low", "medium", "high", "critical"] = Field(
        description="Issue severity level"
    )
    category: str = Field(
        description="Issue category: correctness, safety, regression, completeness, performance_regression"
    )
    description: str = Field(description="Human-readable description of the issue")
    suggestion: str = Field(default="", description="Fix suggestion")


class CheckerOutput(BaseModel):
    """Structured output from the checker agent."""
    status: Literal["PASS", "WARN", "FAIL"] = Field(
        description="PASS if no issues, WARN if only low/medium issues, FAIL if any high/critical issues"
    )
    issues: list[CheckerIssue] = Field(
        default_factory=list,
        description="List of all issues found in the sandbox codebase"
    )
    summary: str = Field(
        default="",
        description="One-paragraph summary of the check results"
    )
