"""Pydantic output schema for the verifier agent."""
from typing import Literal
from pydantic import BaseModel, Field

from src.code_rewriter.models.feedback_models import RepairIssue


class VerifierOutput(BaseModel):
    status: Literal["PASS", "FAIL"] = Field(description="PASS if the sandbox app started cleanly, FAIL otherwise")
    category: Literal[
        "strategy_not_applied",
        "not_executable",
        "name_error",
        "syntax_error",
        "args_required",
        "NONE",
    ] = Field(
        default="NONE",
        description="Failure category: strategy_not_applied, not_executable, name_error, syntax_error, args_required. Use NONE for PASS or env-only failures (no DB server, no network).",
    )
    reason: str = Field(default="", description="One-line explanation")
    detail: str = Field(default="", description="Specific error location and fix hint if FAIL")
    suggestion: str = Field(
        default="",
        description="Optional improvement suggestion for the code optimizer. "
                    "Provide a concise, actionable fix hint ONLY when the optimizer "
                    "needs to improve something. Leave empty when the code is correct "
                    "and no changes are needed.",
    )
    issues: list[RepairIssue] = Field(
        default_factory=list,
        description="Evidence-backed semantic issues. Each issue MUST carry a "
                    "non-empty `evidence` string quoting the offending code line, "
                    "diff line, deterministic violation code, or resolved SQL that "
                    "proves the problem. Findings without evidence are dropped.",
    )