"""Pydantic models for knob_recommender sub-agent."""

from typing import Any

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


# Mapping to tools/experiments.py (workflow converts trivially):
#   DesignedLevel -> ExperimentLevel (knob/value/reasoning identical)
#   DesignedArm -> ExperimentArm (name/phase/levels/rationale identical;
#     phase validity enforced in tools.write_experiment_protocol, not here)
#   ExperimentDesignOutput -> ExperimentProtocol (objective/arms/summary identical)


class DesignedLevel(BaseModel):
    """One knob setting within a designed experiment arm."""

    knob: str = Field(description="Name of the database configuration knob/parameter")
    value: Any = Field(description="Proposed level value for this arm")
    reasoning: str = Field(default="", description="DBA rationale for this level")


class DesignedArm(BaseModel):
    """One arm of a designed experiment protocol."""

    name: str = Field(description="Unique arm name")
    phase: str = Field(description="Experiment phase: screen, interaction, or refinement")
    levels: list[DesignedLevel] = Field(description="Knob-level assignments for this arm")
    rationale: str = Field(default="", description="DBA rationale for this arm")


class ExperimentDesignOutput(BaseModel):
    """Structured output from the experiment-designer agent."""

    objective: str = Field(default="", description="Tuning objective for this protocol")
    arms: list[DesignedArm] = Field(
        default_factory=list, description="Designed experiment arms across phases"
    )
    summary: str = Field(default="", description="Executive summary of the protocol")


class ProposedLevel(BaseModel):
    """One knob setting within a proposed single experiment."""

    knob: str = Field(description="Name of the database configuration knob/parameter")
    value: Any = Field(description="Proposed level value for this experiment")
    reasoning: str = Field(default="", description="DBA rationale for this level")


class ExperimentProposal(BaseModel):
    """Structured output: the single next sequential experiment."""

    objective: str = Field(default="", description="Tuning objective for this experiment")
    name: str = Field(description="Unique experiment name")
    phase: str = Field(description="Experiment phase: screen, interaction, or refinement")
    levels: list[ProposedLevel] = Field(description="Knob-level assignments")
    rationale: str = Field(default="", description="DBA rationale for this experiment")
