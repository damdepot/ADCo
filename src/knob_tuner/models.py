from typing import Any, Dict
from pydantic import BaseModel, Field

class TunerOutputs(BaseModel):
    db_inspector: Dict[str, Any] = Field(default_factory=dict)
    intent_analyzer: Dict[str, Any] = Field(default_factory=dict)
    knob_recommender: Dict[str, Any] = Field(default_factory=dict)
    knob_checker: Dict[str, Any] = Field(default_factory=dict)
    live_tuner: Dict[str, Any] = Field(default_factory=dict)

class KnobTunerResult(BaseModel):
    timestamp: str
    target: str
    db_type: str
    db_name: str
    cpu_cores: Any
    memory_gb: Any
    production_db: bool = False
    dry_run: bool = False
    staging_validated: bool = False
    status: str = "UNKNOWN"
    validation_attempt_count: int = 0
    intent_analyzer_output: Dict[str, Any] = Field(default_factory=dict)
    knob_recommender_output: Dict[str, Any] = Field(default_factory=dict)
    knob_checker_output: Dict[str, Any] = Field(default_factory=dict)
    live_tuner_output: Dict[str, Any] = Field(default_factory=dict)
    outputs: TunerOutputs = Field(default_factory=TunerOutputs)
