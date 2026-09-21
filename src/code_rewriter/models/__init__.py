from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from .ast_models import (
    CallAnalysis,
    ClassAnalysis,
    ControlFlowAnalysis,
    DatabaseOperation,
    DbOperationType,
    FileAnalysis,
    FunctionAnalysis,
    ImportAnalysis,
    SourceLocation,
    SqlOperation,
    StructuralSummary,
)
from .rewrite_models import (
    RewriteContract,
    RewriteTarget,
)

class RewriterOutputs(BaseModel):
    scan_result: Dict[str, Any] = Field(default_factory=dict)
    file_selector_output: Dict[str, Any] = Field(default_factory=dict)
    intent_output: Dict[str, Any] = Field(default_factory=dict)
    intent_extractor_output: Dict[str, Any] = Field(default_factory=dict)
    code_optimizer_output: Dict[str, Any] = Field(default_factory=dict)
    verifier_output: Dict[str, Any] = Field(default_factory=dict)

class CodeRewriterResult(BaseModel):
    timestamp: str
    target: str
    model: str
    sandbox: Optional[str] = None
    status: str = "FAIL"
    modified_files: List[str] = Field(default_factory=list)
    outputs: RewriterOutputs = Field(default_factory=RewriterOutputs)


__all__ = [
    "SourceLocation",
    "ControlFlowAnalysis",
    "CallAnalysis",
    "SqlOperation",
    "DbOperationType",
    "DatabaseOperation",
    "ImportAnalysis",
    "FunctionAnalysis",
    "ClassAnalysis",
    "StructuralSummary",
    "FileAnalysis",
    "RewriteTarget",
    "RewriteContract",
    "RewriterOutputs",
    "CodeRewriterResult",
]
