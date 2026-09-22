from typing import Optional

from pydantic import BaseModel, Field

from .ast_models import SourceLocation


class RewriteTarget(BaseModel):
    """Specifies the target of a rewrite operation."""
    file: str = Field(description="The absolute or relative path to the target file")
    function: Optional[str] = Field(default=None, description="The specific function to target, if applicable")
    qualified_function: Optional[str] = Field(default=None, description="The fully qualified name of the target function, if applicable")
    source_location: Optional[SourceLocation] = Field(default=None, description="The source location of the target, if applicable")


class RewriteContract(BaseModel):
    """A contract defining constraints and strategies for a specific code rewrite."""
    rewrite_id: str = Field(description="A unique identifier for this rewrite operation")
    target: RewriteTarget = Field(description="The target file or function for this rewrite")
    targets: list[RewriteTarget] = Field(default_factory=list, description="Optional list of all expected rewrite targets. When non-empty, the verifier checks coverage across all targets.")
    pattern: str = Field(description="The optimization or design pattern to apply")
    strategy: str = Field(description="The strategy detailing how the rewrite will be performed")
    allowed_regions: list[str] = Field(default_factory=list, description="Regions of the target that are allowed to be modified")
    must_preserve: list[str] = Field(default_factory=list, description="Features or behaviors that must be preserved after the rewrite")
    must_not_change: list[str] = Field(default_factory=list, description="Features or behaviors that must explicitly not be changed")
