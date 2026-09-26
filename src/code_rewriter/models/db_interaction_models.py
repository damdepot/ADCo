from typing import List, Optional, Tuple

from pydantic import BaseModel, Field


class SqlModel(BaseModel):
    """Static shape of a single resolved SQL statement.

    The model is intentionally schema-agnostic: it only records structure that
    can be derived from the parsed statement itself (relations, joins,
    aggregates, placeholders) and never relies on knowledge of specific tables
    or columns.
    """

    tables_read: List[str] = Field(default_factory=list, description="Tables referenced by read statements")
    tables_written: List[str] = Field(default_factory=list, description="Tables mutated by write statements")
    top_level_relations: int = Field(default=0, description="Table sources in the outermost FROM + JOIN list")
    join_count: int = Field(default=0, description="Joins at the top level of the outermost query")
    has_aggregate: bool = Field(default=False, description="Whether the top-level query uses an aggregate function")
    aggregate_funcs: List[str] = Field(default_factory=list, description="Names of the aggregate functions used")
    has_distinct: bool = Field(default=False, description="Whether the statement uses DISTINCT")
    has_subquery: bool = Field(default=False, description="Whether the statement contains a nested subquery")
    placeholder_count: int = Field(default=0, description="Number of neutral '?' placeholders after normalization")
    parse_ok: bool = Field(default=True, description="Whether sqlglot parsed the statement")


class StatementModel(BaseModel):
    """A single cursor execution inside a function, in program order."""

    index: int = Field(description="Zero-based position among the function's executions")
    sql: str = Field(description="Resolved SQL string for the execution")
    sql_operation: str = Field(description="Leading SQL verb (SELECT/INSERT/UPDATE/DELETE/OTHER)")
    source_line: int = Field(description="Line number of the execute() call")
    model: Optional[SqlModel] = Field(default=None, description="Parsed structural model, if parseable")


class FunctionDbModel(BaseModel):
    """Database interaction model for one function."""

    function: str = Field(default="", description="Qualified function name")
    file: str = Field(default="", description="Source file the function belongs to")
    statements: List[StatementModel] = Field(default_factory=list, description="Executions in program order")
    value_edges: List[Tuple[int, int]] = Field(
        default_factory=list,
        description="Producer statement index -> consumer statement index value dependencies",
    )
    commit_positions: List[int] = Field(
        default_factory=list,
        description="Statement indices after which a commit/rollback occurs",
    )
