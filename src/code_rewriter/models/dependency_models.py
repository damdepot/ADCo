"""Dependency Graph and Slicing Models for ADCo."""

from enum import StrEnum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from .ast_models import SourceLocation

class DependencyType(StrEnum):
    CALL = "CALL"
    CLASS_STATE = "CLASS_STATE"
    IMPORT = "IMPORT"
    DATABASE_OPERATION = "DATABASE_OPERATION"

class CertaintyLevel(StrEnum):
    EXACT = "EXACT"
    INFERRED = "INFERRED"

class NodeKind(StrEnum):
    FUNCTION = "FUNCTION"
    METHOD = "METHOD"
    CLASS = "CLASS"
    IMPORT = "IMPORT"
    DATABASE_OPERATION = "DATABASE_OPERATION"

class DependencyNode(BaseModel):
    id: str = Field(description="Deterministic unique ID, e.g. 'drivers/postgresdriver.py::PostgresDriver.doDelivery'")
    name: str = Field(description="Short name of the entity")
    kind: NodeKind = Field(description="Kind of node")
    file_path: str = Field(description="Relative or absolute file path")
    parent_id: Optional[str] = Field(default=None, description="ID of parent class or module")
    code_snippet: Optional[str] = Field(default=None, description="Verbatim code definition if extracted")
    source_location: Optional[SourceLocation] = Field(default=None)
    metadata: Dict[str, Any] = Field(default_factory=dict)

class DependencyEdge(BaseModel):
    edge_id: str = Field(description="Unique edge identifier")
    source_id: str = Field(description="Source node ID")
    target_id: str = Field(description="Target node ID")
    kind: DependencyType = Field(description="Type of dependency")
    certainty: CertaintyLevel = Field(default=CertaintyLevel.EXACT)
    source_location: Optional[SourceLocation] = Field(default=None)
    metadata: Dict[str, Any] = Field(default_factory=dict)

class DependencyGraph(BaseModel):
    nodes: Dict[str, DependencyNode] = Field(default_factory=dict)
    edges: List[DependencyEdge] = Field(default_factory=list)

    def add_node(self, node: DependencyNode) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: DependencyEdge) -> None:
        self.edges.append(edge)

class DependencySlice(BaseModel):
    target_id: str
    target_node: DependencyNode
    sliced_nodes: Dict[str, DependencyNode] = Field(default_factory=dict)
    sliced_edges: List[DependencyEdge] = Field(default_factory=list)
    class_context: Optional[DependencyNode] = None
    database_operations: List[Dict[str, Any]] = Field(default_factory=list)
    state_attributes: List[str] = Field(default_factory=list)
    relevant_imports: List[str] = Field(default_factory=list)
    referenced_queries: Dict[str, str] = Field(default_factory=dict)
    is_truncated: bool = False
