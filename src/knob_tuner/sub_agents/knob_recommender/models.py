"""Pydantic models for knob_recommender sub-agent."""

from typing import Any, Literal
from pydantic import BaseModel, Field


class KnobRecommendation(BaseModel):
    """A single database configuration knob recommendation."""

    knob: str = Field(description="Name of the database configuration knob/parameter")
    current_value: str = Field(description="Current value before tuning")
    recommended_value: str = Field(description="Recommended tuned value")
    unit: str = Field(default="", description="Unit of measurement if applicable (e.g., MB, kB, s, ms)")
    reasoning: str = Field(
        description="Detailed DBA rationale for this recommendation based on workload, hardware, and formula"
    )
    restart_required: bool = Field(
        default=False,
        description="Whether applying this knob requires a database server restart",
    )
    risk_level: Literal["low", "medium", "high"] = Field(
        default="low",
        description="Risk level associated with applying this recommendation (low, medium, high)",
    )


class KnobRecommenderOutput(BaseModel):
    """Structured output from the knob recommender agent."""

    total_memory_allocated_gb: float = Field(
        default=0.0,
        description="Total estimated memory allocated across all memory knobs in GB",
    )
    memory_budget_pct: float = Field(
        default=0.0,
        description="Percentage of total system/container memory utilized by recommendations (0.0 to 100.0)",
    )
    recommendations: list[KnobRecommendation] = Field(
        default_factory=list,
        description="List of recommended knob changes",
    )
    summary: str = Field(
        default="",
        description="Executive DBA summary explaining the overall tuning strategy and expected impact",
    )
    restart_required: bool = Field(
        default=False,
        description="Whether any of the recommended knobs require a database restart",
    )
