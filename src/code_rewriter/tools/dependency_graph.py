from collections import deque
from typing import Any, Dict, List, Optional

from ..models.ast_models import FileAnalysis, FunctionAnalysis, SourceLocation
from ..models.dependency_models import (
    CertaintyLevel,
    DependencyEdge,
    DependencyGraph,
    DependencyNode,
    DependencySlice,
    DependencyType,
    NodeKind,
)


def _extract_snippet(source_code: Optional[str], loc: Optional[SourceLocation]) -> Optional[str]:
    """Extracts a source code snippet from the given source code and source location."""
    if not source_code or not loc or loc.start_line <= 0:
        return None
    lines = source_code.splitlines()
    start = max(0, loc.start_line - 1)
    end = min(len(lines), loc.end_line)
    return "\n".join(lines[start:end])


def build_dependency_graph(analysis: FileAnalysis, source_code: Optional[str] = None) -> DependencyGraph:
    """
    Builds a DependencyGraph from a FileAnalysis IR.

    Nodes:
      - Imports (NodeKind.IMPORT)
      - Classes (NodeKind.CLASS)
      - Methods (NodeKind.METHOD)
      - Functions (NodeKind.FUNCTION)
      - Database Operations (NodeKind.DATABASE_OPERATION)

    Edges:
      - CALL: between caller functions/methods and callee functions/methods or imports.
      - CLASS_STATE: when receiver is 'self' or driver attributes (e.g., self.cursor, self.conn).
      - DATABASE_OPERATION: linking a function/method to its DatabaseOperation IR items.
      - IMPORT: linking functions to imported driver helpers or direct imports.
    """
    graph = DependencyGraph()
    file_path = analysis.file_path or ""

    # 1. Imports
    import_by_name: Dict[str, str] = {}
    for imp in analysis.imports:
        bound_name = imp.alias or imp.name
        node_id = f"{file_path}::import::{bound_name}"
        node = DependencyNode(
            id=node_id,
            name=bound_name,
            kind=NodeKind.IMPORT,
            file_path=file_path,
            source_location=imp.source_location,
            code_snippet=_extract_snippet(source_code, imp.source_location),
            metadata={
                "import_type": imp.import_type,
                "module": imp.module,
                "imported_name": imp.name,
                "alias": imp.alias,
            },
        )
        graph.add_node(node)
        import_by_name[bound_name] = node_id
        if imp.module:
            import_by_name[imp.module] = node_id

    # 2. Classes
    class_by_name: Dict[str, str] = {}
    for cls in analysis.classes:
        cls_id = f"{file_path}::{cls.name}"
        class_node = DependencyNode(
            id=cls_id,
            name=cls.name,
            kind=NodeKind.CLASS,
            file_path=file_path,
            source_location=cls.source_location,
            code_snippet=_extract_snippet(source_code, cls.source_location),
            metadata={"base_classes": cls.base_classes},
        )
        graph.add_node(class_node)
        class_by_name[cls.name] = cls_id

    # 3. Class Methods
    all_functions_with_context: List[tuple[FunctionAnalysis, Optional[str], str]] = []
    for cls in analysis.classes:
        cls_id = f"{file_path}::{cls.name}"
        for fn in cls.methods:
            fn_id = f"{file_path}::{cls.name}.{fn.name}"
            method_node = DependencyNode(
                id=fn_id,
                name=fn.name,
                kind=NodeKind.METHOD,
                file_path=file_path,
                parent_id=cls_id,
                source_location=fn.source_location,
                code_snippet=_extract_snippet(source_code, fn.source_location),
                metadata={
                    "qualified_name": fn.qualified_name,
                    "parameters": fn.parameters,
                    "decorators": fn.decorators,
                    "return_count": fn.return_count,
                    "class_name": cls.name,
                },
            )
            graph.add_node(method_node)
            all_functions_with_context.append((fn, cls.name, fn_id))

    # 4. Top-level Functions
    for fn in analysis.functions:
        fn_id = f"{file_path}::{fn.name}"
        func_node = DependencyNode(
            id=fn_id,
            name=fn.name,
            kind=NodeKind.FUNCTION,
            file_path=file_path,
            source_location=fn.source_location,
            code_snippet=_extract_snippet(source_code, fn.source_location),
            metadata={
                "qualified_name": fn.qualified_name,
                "parameters": fn.parameters,
                "decorators": fn.decorators,
                "return_count": fn.return_count,
            },
        )
        graph.add_node(func_node)
        all_functions_with_context.append((fn, None, fn_id))

    # 5. Database Operations & Edges
    for fn, cls_name, fn_id in all_functions_with_context:
        for idx, db_op in enumerate(fn.database_operations):
            db_node_id = f"{fn_id}::db_op::{idx + 1}"
            db_node = DependencyNode(
                id=db_node_id,
                name=db_op.call_name,
                kind=NodeKind.DATABASE_OPERATION,
                file_path=file_path,
                parent_id=fn_id,
                source_location=db_op.source_location,
                code_snippet=_extract_snippet(source_code, db_op.source_location),
                metadata={
                    "call_name": db_op.call_name,
                    "operation_type": db_op.operation_type,
                    "sql_operation": db_op.sql_operation,
                    "sql": db_op.sql,
                    "inside_loop": db_op.inside_loop,
                    "loop_variables": db_op.loop_variables,
                    "parameter_dependencies": db_op.parameter_dependencies,
                },
            )
            graph.add_node(db_node)

            edge = DependencyEdge(
                edge_id=f"{fn_id}->{db_node_id}",
                source_id=fn_id,
                target_id=db_node_id,
                kind=DependencyType.DATABASE_OPERATION,
                certainty=CertaintyLevel.EXACT,
                source_location=db_op.source_location,
            )
            graph.add_edge(edge)

        # 6. Calls & CLASS_STATE / IMPORT Edges
        for call_idx, call in enumerate(fn.calls):
            if call.receiver == "self":
                # Check if it's calling another method on the same class
                method_target_id = f"{file_path}::{cls_name}.{call.call_name}" if cls_name else None
                if method_target_id and method_target_id in graph.nodes:
                    graph.add_edge(
                        DependencyEdge(
                            edge_id=f"{fn_id}->{method_target_id}::call::{call_idx}",
                            source_id=fn_id,
                            target_id=method_target_id,
                            kind=DependencyType.CALL,
                            certainty=CertaintyLevel.EXACT,
                            source_location=call.source_location,
                            metadata={"call_name": call.call_name, "receiver": call.receiver},
                        )
                    )
                elif cls_name:
                    # Accessing class state (e.g., self.cursor, self.conn, self.db, self.helper)
                    cls_id = f"{file_path}::{cls_name}"
                    graph.add_edge(
                        DependencyEdge(
                            edge_id=f"{fn_id}->{cls_id}::state::{call.call_name}::{call_idx}",
                            source_id=fn_id,
                            target_id=cls_id,
                            kind=DependencyType.CLASS_STATE,
                            certainty=CertaintyLevel.INFERRED,
                            source_location=call.source_location,
                            metadata={"receiver": "self", "attribute": f"self.{call.call_name}"},
                        )
                    )
            elif call.receiver:
                # E.g. db.execute, cursor.execute, conn.commit, module.func
                if call.receiver in import_by_name:
                    imp_id = import_by_name[call.receiver]
                    graph.add_edge(
                        DependencyEdge(
                            edge_id=f"{fn_id}->{imp_id}::call::{call_idx}",
                            source_id=fn_id,
                            target_id=imp_id,
                            kind=DependencyType.IMPORT,
                            certainty=CertaintyLevel.EXACT,
                            source_location=call.source_location,
                            metadata={"call_name": call.call_name, "receiver": call.receiver},
                        )
                    )
                elif cls_name and call.receiver in (
                    "cursor",
                    "conn",
                    "connection",
                    "db",
                    "session",
                    "pool",
                ):
                    cls_id = f"{file_path}::{cls_name}"
                    graph.add_edge(
                        DependencyEdge(
                            edge_id=f"{fn_id}->{cls_id}::driver::{call.receiver}::{call_idx}",
                            source_id=fn_id,
                            target_id=cls_id,
                            kind=DependencyType.CLASS_STATE,
                            certainty=CertaintyLevel.INFERRED,
                            source_location=call.source_location,
                            metadata={"receiver": call.receiver, "attribute": f"self.{call.receiver}"},
                        )
                    )
            else:
                # Receiver is None (direct function call)
                top_fn_id = f"{file_path}::{call.call_name}"
                cls_fn_id = f"{file_path}::{cls_name}.{call.call_name}" if cls_name else None

                if top_fn_id in graph.nodes:
                    graph.add_edge(
                        DependencyEdge(
                            edge_id=f"{fn_id}->{top_fn_id}::call::{call_idx}",
                            source_id=fn_id,
                            target_id=top_fn_id,
                            kind=DependencyType.CALL,
                            certainty=CertaintyLevel.EXACT,
                            source_location=call.source_location,
                            metadata={"call_name": call.call_name},
                        )
                    )
                elif cls_fn_id and cls_fn_id in graph.nodes:
                    graph.add_edge(
                        DependencyEdge(
                            edge_id=f"{fn_id}->{cls_fn_id}::call::{call_idx}",
                            source_id=fn_id,
                            target_id=cls_fn_id,
                            kind=DependencyType.CALL,
                            certainty=CertaintyLevel.EXACT,
                            source_location=call.source_location,
                            metadata={"call_name": call.call_name},
                        )
                    )
                elif call.call_name in import_by_name:
                    imp_id = import_by_name[call.call_name]
                    graph.add_edge(
                        DependencyEdge(
                            edge_id=f"{fn_id}->{imp_id}::import::{call_idx}",
                            source_id=fn_id,
                            target_id=imp_id,
                            kind=DependencyType.IMPORT,
                            certainty=CertaintyLevel.EXACT,
                            source_location=call.source_location,
                            metadata={"call_name": call.call_name},
                        )
                    )

        # 7. Driver helper imports if function contains DB operations
        if fn.database_operations:
            driver_keywords = {
                "sqlite3",
                "psycopg2",
                "psycopg",
                "mysql",
                "sqlalchemy",
                "asyncpg",
                "aiomysql",
                "databases",
            }
            for imp in analysis.imports:
                imp_name = imp.alias or imp.name
                imp_id = f"{file_path}::import::{imp_name}"
                if (imp.module and any(k in imp.module for k in driver_keywords)) or (
                    imp.name and any(k in imp.name for k in driver_keywords)
                ):
                    if imp_id in graph.nodes:
                        edge_id = f"{fn_id}->{imp_id}::driver_import"
                        if not any(e.edge_id == edge_id for e in graph.edges):
                            graph.add_edge(
                                DependencyEdge(
                                    edge_id=edge_id,
                                    source_id=fn_id,
                                    target_id=imp_id,
                                    kind=DependencyType.IMPORT,
                                    certainty=CertaintyLevel.INFERRED,
                                    metadata={"role": "database_driver"},
                                )
                            )

    return graph


def _resolve_target_node_id(graph: DependencyGraph, target_id: str) -> str:
    """Finds the matching node ID in the graph given a full ID, function name, or qualified name."""
    if target_id in graph.nodes:
        return target_id

    # Search by exact name or qualified_name or suffix match
    for node_id, node in graph.nodes.items():
        if node.name == target_id:
            return node_id
        if node.metadata.get("qualified_name") == target_id:
            return node_id
        if node_id.endswith(f"::{target_id}") or node_id.endswith(f".{target_id}"):
            return node_id

    raise ValueError(f"Target node '{target_id}' not found in dependency graph")


def slice_dependency_graph(
    graph: DependencyGraph,
    target_id: str,
    max_depth: int = 3,
    max_nodes: int = 40,
) -> DependencySlice:
    """
    Performs a bounded BFS starting from target_id traversing outgoing edges with cycle detection.
    Gathers class context, database operations, state attributes, callee signatures, imports, and referenced queries.
    """
    actual_target_id = _resolve_target_node_id(graph, target_id)
    target_node = graph.nodes[actual_target_id]

    # Bounded BFS
    queue: deque[tuple[str, int]] = deque([(actual_target_id, 0)])
    visited: set[str] = {actual_target_id}
    sliced_nodes: Dict[str, DependencyNode] = {actual_target_id: target_node}
    sliced_edges: List[DependencyEdge] = []
    is_truncated = False

    while queue:
        curr_id, curr_depth = queue.popleft()

        if curr_depth >= max_depth:
            continue

        # Find outgoing edges from curr_id
        for edge in graph.edges:
            if edge.source_id == curr_id:
                next_id = edge.target_id
                if edge not in sliced_edges:
                    sliced_edges.append(edge)

                if next_id in graph.nodes:
                    if next_id not in visited:
                        if len(sliced_nodes) >= max_nodes:
                            is_truncated = True
                            break
                        visited.add(next_id)
                        sliced_nodes[next_id] = graph.nodes[next_id]
                        queue.append((next_id, curr_depth + 1))

    # Determine class context
    class_context: Optional[DependencyNode] = None
    if target_node.parent_id and target_node.parent_id in graph.nodes:
        class_context = graph.nodes[target_node.parent_id]
    elif target_node.kind == NodeKind.CLASS:
        class_context = target_node

    # Gather database operations
    db_ops: List[Dict[str, Any]] = []
    referenced_queries: Dict[str, str] = {}
    for node in sliced_nodes.values():
        if node.kind == NodeKind.DATABASE_OPERATION:
            op_dict = {
                "id": node.id,
                "call_name": node.metadata.get("call_name", node.name),
                "operation_type": node.metadata.get("operation_type"),
                "sql_operation": node.metadata.get("sql_operation"),
                "sql": node.metadata.get("sql"),
                "inside_loop": node.metadata.get("inside_loop", False),
                "loop_variables": node.metadata.get("loop_variables", []),
                "parameter_dependencies": node.metadata.get("parameter_dependencies", []),
                "source_location": node.source_location.model_dump() if node.source_location else None,
                "code_snippet": node.code_snippet,
            }
            db_ops.append(op_dict)
            if node.metadata.get("sql"):
                referenced_queries[node.id] = node.metadata["sql"]

    # Gather state attributes
    state_attributes: List[str] = []
    seen_state: set[str] = set()
    for edge in sliced_edges:
        if edge.kind == DependencyType.CLASS_STATE:
            attr = edge.metadata.get("attribute") or edge.metadata.get("receiver")
            if attr and attr not in seen_state:
                seen_state.add(attr)
                state_attributes.append(attr)

    # Gather relevant imports
    relevant_imports: List[str] = []
    for node in sliced_nodes.values():
        if node.kind == NodeKind.IMPORT:
            imp_type = node.metadata.get("import_type")
            module = node.metadata.get("module")
            imp_name = node.metadata.get("imported_name", node.name)
            alias = node.metadata.get("alias")

            if node.code_snippet:
                import_stmt = node.code_snippet.strip()
            elif imp_type == "from" and module:
                import_stmt = f"from {module} import {imp_name}" + (f" as {alias}" if alias else "")
            else:
                import_stmt = f"import {imp_name}" + (f" as {alias}" if alias else "")

            if import_stmt not in relevant_imports:
                relevant_imports.append(import_stmt)

    return DependencySlice(
        target_id=actual_target_id,
        target_node=target_node,
        sliced_nodes=sliced_nodes,
        sliced_edges=sliced_edges,
        class_context=class_context,
        database_operations=db_ops,
        state_attributes=state_attributes,
        relevant_imports=relevant_imports,
        referenced_queries=referenced_queries,
        is_truncated=is_truncated,
    )


def format_dependency_slice_markdown(slice_data: DependencySlice, contract: Optional[Any] = None) -> str:
    """
    Produces clean, readable Markdown for LLM prompt context formatting:
      - Target function source
      - Class State & Drivers / Enclosing Class Context
      - Database Operations (flagging loop N+1 operations)
      - Helper definitions and referenced symbols
      - Contract & Strategy directives
    """
    target = slice_data.target_node
    lines = [
        f"# Dependency Slice: `{target.name}`\n",
        "## Target Definition",
        f"- **ID**: `{target.id}`",
        f"- **File**: `{target.file_path}`",
        f"- **Kind**: `{target.kind}`",
    ]
    if target.source_location:
        lines.append(f"- **Lines**: {target.source_location.start_line}-{target.source_location.end_line}")

    snippet = target.code_snippet or "# (No source snippet available)"
    lines.extend([
        "",
        "```python",
        snippet,
        "```",
    ])

    if slice_data.class_context or slice_data.state_attributes:
        cls_name = slice_data.class_context.name if slice_data.class_context else "N/A"
        lines.extend([
            "",
            "## Enclosing Class Context",
            f"- **Class**: `{cls_name}`",
        ])
        if slice_data.state_attributes:
            lines.append(f"- **State Attributes / Drivers**: {', '.join(f'`{attr}`' for attr in slice_data.state_attributes)}")
        if slice_data.class_context and slice_data.class_context.code_snippet:
            lines.extend([
                "",
                "```python",
                slice_data.class_context.code_snippet,
                "```",
            ])

    # Database Operations
    db_ops = slice_data.database_operations
    lines.extend([
        "",
        f"## Database Operations ({len(db_ops)})",
    ])
    if db_ops:
        for idx, op in enumerate(db_ops, start=1):
            inside_loop = op.get("inside_loop", False)
            loop_flag = "⚠️ YES [LOOP / N+1 RISK]" if inside_loop else "False"
            call_name = op.get("call_name", "db_call")
            op_type = op.get("operation_type", "UNKNOWN")
            sql_op = op.get("sql_operation", "UNKNOWN")
            sql = op.get("sql")

            lines.extend([
                f"### Operation {idx}: `{call_name}` ({op_type})",
                f"- **SQL Operation**: `{sql_op}`",
                f"- **Inside Loop**: {loop_flag}",
            ])
            loop_vars = op.get("loop_variables", [])
            if loop_vars:
                lines.append(f"- **Loop Variables**: {', '.join(f'`{v}`' for v in loop_vars)}")
            param_deps = op.get("parameter_dependencies", [])
            if param_deps:
                lines.append(f"- **Parameter Dependencies**: {', '.join(f'`{p}`' for p in param_deps)}")
            if sql:
                lines.extend([
                    "- **SQL Statement**:",
                    "```sql",
                    sql.strip(),
                    "```",
                ])
    else:
        lines.append("No direct database operations detected in target slice.")

    # Helper functions & Callees
    callee_nodes = [
        node for node_id, node in slice_data.sliced_nodes.items()
        if node_id != slice_data.target_id and node.kind in (NodeKind.FUNCTION, NodeKind.METHOD)
    ]
    if callee_nodes or slice_data.relevant_imports:
        lines.extend([
            "",
            "## Callees & Helper Functions",
        ])
        for node in callee_nodes:
            params = node.metadata.get("parameters", [])
            sig = f"{node.name}({', '.join(params)})"
            snippet = node.code_snippet or f"def {sig}: ..."
            lines.extend([
                f"### `{sig}`:",
                "```python",
                snippet,
                "```",
            ])
        if slice_data.relevant_imports:
            lines.extend([
                "",
                "### Relevant Imports:",
                *(f"- `{imp}`" for imp in slice_data.relevant_imports),
            ])

    # Optimization Contract & Strategy Directives
    if contract:
        pattern = getattr(contract, "pattern", None) or (contract.get("pattern") if isinstance(contract, dict) else "N/A")
        strategy = getattr(contract, "strategy", None) or (contract.get("strategy") if isinstance(contract, dict) else "N/A")
        allowed_regions = getattr(contract, "allowed_regions", []) or (contract.get("allowed_regions", []) if isinstance(contract, dict) else [])
        must_preserve = getattr(contract, "must_preserve", []) or (contract.get("must_preserve", []) if isinstance(contract, dict) else [])
        must_not_change = getattr(contract, "must_not_change", []) or (contract.get("must_not_change", []) if isinstance(contract, dict) else [])

        lines.extend([
            "",
            "## Optimization Contract",
            f"- **Optimization Pattern**: {pattern}",
            f"- **Proposed Strategy**: {strategy}",
        ])
        if allowed_regions:
            lines.append(f"- **Allowed Edit Regions**: {', '.join(f'`{r}`' for r in allowed_regions)}")
        if must_preserve:
            lines.extend([
                "- **Must Preserve Invariants**:",
                *(f"  - {p}" for p in must_preserve),
            ])
        if must_not_change:
            lines.extend([
                "- **Must Not Change Invariants**:",
                *(f"  - {n}" for n in must_not_change),
            ])

    return "\n".join(lines) + "\n"
