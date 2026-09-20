"""Pydantic output schema for the intent extractor agent."""
from pydantic import BaseModel, Field


class WorkloadPattern(BaseModel):
    query_types: list[str] = Field(
        default_factory=list,
        description="List of detected query types: SELECT, INSERT, UPDATE, DELETE, JOIN, GROUP_BY, AGGREGATION, etc.",
    )
    orm_detected: str = Field(
        default="Raw SQL / Direct Driver",
        description="Detected ORM or framework: SQLAlchemy, Django ORM, Hibernate, GORM, Prisma, Peewee, etc.",
    )
    transaction_pattern: str = Field(
        default="Auto-commit / Implicit",
        description="Transaction management pattern: Explicit commit/rollback, Context-Managed, @Transactional, etc.",
    )
    estimated_read_write_ratio: str = Field(
        default="",
        description="Estimated read vs write ratio e.g. '80% Read / 20% Write (Read-Heavy)'",
    )
    notable_patterns: list[str] = Field(
        default_factory=list,
        description="Notable patterns: N+1 query loops, bulk batching, connection pooling, async DB access, analytics",
    )


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
    workload: WorkloadPattern = Field(
        default_factory=WorkloadPattern,
        description="Structured workload characteristics extracted for database configuration tuning",
    )
    optimization_targets: list[OptimizationTarget] = Field(
        default_factory=list,
        description="Files with concrete optimization opportunities",
    )
    notes: str = Field(default="", description="Additional observations")