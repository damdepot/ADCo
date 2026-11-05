from typing import Any, Literal, Optional
from pydantic import BaseModel, Field

VerificationStatus = Literal["PASS", "FAIL"]
ViolationSeverity = Literal["ERROR", "WARNING"]
CheckStatus = Literal["PASS", "FAIL", "SKIPPED"]


class VerificationGap(BaseModel):
    """A deterministic check that could not evaluate its input.

    Gaps are not violations: the rewrite may still be correct, but the check
    silently skipped the site, so the result is only PARTIAL evidence.
    """

    check: str
    function: str = ""
    line: Optional[int] = None
    reason: str

class VerificationViolation(BaseModel):
    code: str
    severity: ViolationSeverity
    message: str
    expected: Optional[Any] = None
    actual: Optional[Any] = None

class VerificationCheck(BaseModel):
    name: str
    status: CheckStatus
    details: str

class TargetStatus(BaseModel):
    file: str = Field(description="The file containing the target")
    function: str = Field(description="The function name")
    status: Literal[
        "TRANSFORMED", "MISSING_REWRITE", "INVALID_REWRITE", "UNVERIFIABLE", "RESTRICTED", "NO_OP"
    ] = Field(
        description=(
            "Outcome for this specific target: TRANSFORMED, MISSING_REWRITE, "
            "INVALID_REWRITE, UNVERIFIABLE, RESTRICTED, or NO_OP (loop-free target "
            "with no rewritable pattern, original preserved by design)"
        )
    )
    details: str = Field(default="", description="Explanation of the outcome")

class VerificationResult(BaseModel):
    status: VerificationStatus
    violations: list[VerificationViolation] = Field(default_factory=list)
    checks: list[VerificationCheck] = Field(default_factory=list)
    summary: str
    target_coverage: list[TargetStatus] = Field(default_factory=list, description="Per-target transformation outcomes")
    expected_targets: int = Field(default=0, description="Total expected rewrite targets")
    transformed_targets: int = Field(default=0, description="Successfully transformed targets")
    missing_targets: int = Field(default=0, description="Targets not yet transformed")
    rewrite_coverage: float = Field(default=1.0, description="transformed / expected, in [0.0, 1.0]")
    gaps: list[VerificationGap] = Field(
        default_factory=list,
        description="Checks that could not evaluate a site (PARTIAL evidence)",
    )
    verification_status: Literal["FULL", "PARTIAL"] = Field(
        default="FULL", description="PARTIAL iff any gaps were recorded"
    )
