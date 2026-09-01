"""Pydantic output schema for the verifier agent."""
from typing import Literal
from pydantic import BaseModel, Field


class VerifierOutput(BaseModel):
    status: Literal["PASS", "FAIL"] = Field(description="PASS if the sandbox app started cleanly, FAIL otherwise")
    category: str = Field(
        default="NONE",
        description="Failure category: not_executable, name_error, syntax_error, args_required. Use NONE for PASS or env-only failures (no DB server, no network).",
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