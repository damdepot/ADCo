from typing import Any, Dict
from pydantic import BaseModel, Field

class AdcoPipelineResult(BaseModel):
    timestamp: str
    mode: str
    target: str
    sandbox: str
    model: str
    intent_analyzer: Dict[str, Any] = Field(default_factory=dict)
    rewriter: Dict[str, Any] = Field(default_factory=dict)
    knob_tuner: Dict[str, Any] = Field(default_factory=dict)
