"""Pydantic output schema for the intent extractor agent."""
from pydantic import BaseModel, Field


class OptimizationTarget(BaseModel):
    file: str = Field(description="Relative path to a file that has a concrete optimization opportunity")
    description: str = Field(description="What to optimize in this file and why")


class IntentExtractorOutput(BaseModel):
    connection: str = Field(default="", description="How the app connects to the DB (pool, per-query, singleton, etc.)")
    queries: str = Field(default="", description="SQL/SQL-like operations: CRUD, joins, subqueries, aggregations")
    transactions: str = Field(default="", description="Transaction management pattern")
    n_plus_one: str = Field(default="", description="N+1 risks if any")
    concurrency: str = Field(default="", description="Async, threads, or sequential")
    orm: str = Field(default="", description="ORM usage or raw SQL")
    optimization_targets: list[OptimizationTarget] = Field(
        default_factory=list,
        description="Files with concrete optimization opportunities",
    )
    notes: str = Field(default="", description="Additional observations")