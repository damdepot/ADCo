from typing import Any, Dict
from pydantic import BaseModel, Field

class IntentAnalyzerResult(BaseModel):
    timestamp: str
    target: str
    model: str
    intent_output: Dict[str, Any] = Field(default_factory=dict)
