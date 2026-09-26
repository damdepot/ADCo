from typing import List, Literal

from pydantic import BaseModel, Field


class RiskReport(BaseModel):
    """Advisory structural risk report for a single function rewrite.

    The report compares the database-interaction model of a function before and
    after a rewrite and classifies a conservative structural risk level. It is a
    static risk *proxy* only: it never blocks or rejects a rewrite.
    """

    function: str = Field(default="", description="Qualified function name the report was computed for")
    risk: Literal["LOW", "MEDIUM", "HIGH"] = Field(
        default="LOW", description="Conservative structural risk level"
    )
    flags: List[str] = Field(
        default_factory=list, description="Structural risk flags raised by the comparison"
    )
    evidence: List[str] = Field(
        default_factory=list, description="Human-readable evidence supporting each flag"
    )
    statements_before: int = Field(
        default=0, description="Number of cursor executions in the original function"
    )
    statements_after: int = Field(
        default=0, description="Number of cursor executions in the rewritten function"
    )
    max_relations_before: int = Field(
        default=0, description="Max top-level relations across the original statements"
    )
    max_relations_after: int = Field(
        default=0, description="Max top-level relations across the rewritten statements"
    )
    fused_dependencies: int = Field(
        default=0,
        description="Number of original linear value dependencies that were fused away",
    )
