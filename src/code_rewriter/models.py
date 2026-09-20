from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

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
