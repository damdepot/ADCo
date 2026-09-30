"""Pydantic models for knob_recommender sub-agent."""

from pydantic import BaseModel, Field


class KnobRecommendation(BaseModel):
    """A single database configuration knob recommendation."""

    knob: str = Field(description="Name of the database configuration knob/parameter")
    current_value: str = Field(description="Current value before tuning")
    recommended_value: str = Field(description="Recommended tuned value")
    reasoning: str = Field(
        description="Detailed DBA rationale for this recommendation based on workload, hardware, and formula"
    )
    restart_required: bool = Field(
        default=False,
        description="Whether applying this knob requires a database server restart",
    )


class KnobRecommenderOutput(BaseModel):
    """Structured output from the knob recommender agent."""

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
