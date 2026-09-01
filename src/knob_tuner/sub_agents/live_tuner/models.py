"""Pydantic models for live_tuner sub-agent."""

from typing import Any, Literal
from pydantic import BaseModel, Field


class AppliedKnobDetail(BaseModel):
    """Details of a dynamic knob applied live to production."""

    knob: str = Field(description="Name of the database configuration parameter")
    value: str = Field(description="Parameter value applied to production")
    status: str = Field(default="applied", description="Application status (applied, failed, etc.)")
    error: str | None = Field(default=None, description="Error message if application failed")


class RestartRequiredKnobDetail(BaseModel):
    """Details of a static knob requiring database restart, deferred for maintenance."""

    knob: str = Field(description="Name of the database configuration parameter requiring restart")
    recommended_value: str = Field(
        default="",
        description="Recommended value deferred for scheduled maintenance",
    )
    value: str = Field(
        default="",
        description="Alias for recommended parameter value",
    )
    reasoning: str = Field(default="", description="DBA rationale for recommendation")


class LiveTunerOutput(BaseModel):
    """Structured output from the live tuner agent."""

    status: Literal["APPLIED", "SKIPPED", "PARTIAL", "FAILED"] = Field(
        description="Final status of production tuning: APPLIED (all dynamic knobs applied), PARTIAL (some knobs failed), SKIPPED (staging not validated or all knobs require restart), FAILED (error during execution)"
    )
    applied_knobs: list[AppliedKnobDetail] = Field(
        default_factory=list,
        description="List of dynamic knobs successfully applied live to the production database",
    )
    restart_required_knobs: list[RestartRequiredKnobDetail] = Field(
        default_factory=list,
        description="List of knobs requiring server restart, deferred for scheduled maintenance windows",
    )
    persisted_static_knobs: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of static knobs persisted to configuration for next restart",
    )
    skipped_reason: str = Field(
        default="",
        description="Reason why live application was skipped (e.g. staging validation failed, all knobs static)",
    )
    summary: str = Field(
        default="",
        description="Executive DBA summary of live tuning operations and recommendations for upcoming maintenance",
    )
    next_steps: list[str] = Field(
        default_factory=list,
        description="Recommended operational follow-up actions (e.g. scheduled restart instructions)",
    )
