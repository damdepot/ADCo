"""Pydantic models for intent_analyzer sub-agent."""

from pydantic import BaseModel, Field


class TableInfo(BaseModel):
    name: str = Field(description="Table name")
    columns: list[str] = Field(default_factory=list, description="List of columns with types and nullability")
    indexes: list[str] = Field(default_factory=list, description="List of index names or definitions")
    approximate_row_count: int = Field(default=0, description="Approximate row count in table")


class KnobInfo(BaseModel):
    name: str = Field(description="Knob / parameter name")
    current_value: str = Field(description="Current value of the knob")
    unit: str = Field(default="", description="Unit of measurement (e.g. MB, ms, 8kB)")
    category: str = Field(default="", description="Category of knob (e.g. Memory, Query Tuning, WAL)")
    description: str = Field(default="", description="Description of the knob purpose")
    min_val: str = Field(default="", description="Minimum allowed value")
    max_val: str = Field(default="", description="Maximum allowed value")
    context: str = Field(default="", description="Restart requirement or context (e.g. postmaster, sighup, user)")


class WorkloadPattern(BaseModel):
    query_types: list[str] = Field(default_factory=list, description="Query types detected (SELECT, INSERT, UPDATE, DELETE, etc.)")
    orm_detected: str = Field(default="", description="Detected ORM or data access framework")
    transaction_pattern: str = Field(default="", description="Transaction management pattern")
    estimated_read_write_ratio: str = Field(default="", description="Estimated read vs write ratio")
    notable_patterns: list[str] = Field(default_factory=list, description="Notable patterns like bulk inserts, connection pooling, etc.")


class IntentAnalyzerOutput(BaseModel):
    db_type: str = Field(default="", description="Database engine type (e.g. postgres, mysql)")
    db_version: str = Field(default="", description="Database engine version string")
    cpu_cores: int = Field(default=1, description="Number of CPU cores allocated or available")
    memory_gb: float = Field(default=1.0, description="Total system or container memory in GB")
    tables: list[TableInfo] = Field(default_factory=list, description="Database tables schema information")
    available_knobs: list[KnobInfo] = Field(default_factory=list, description="List of tunable knobs extracted from the database")
    workload: WorkloadPattern = Field(default_factory=WorkloadPattern, description="Workload characteristics extracted from application codebase")
    summary_for_recommender: str = Field(default="", description="Executive summary of workload, schema, and tuning opportunities for the recommender agent")
