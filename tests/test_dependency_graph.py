import pytest
from src.code_rewriter.models.ast_models import FileAnalysis
from src.code_rewriter.models.dependency_models import (
    CertaintyLevel,
    DependencyEdge,
    DependencyGraph,
    DependencyNode,
    DependencyType,
    NodeKind,
)
from src.code_rewriter.models.rewrite_models import RewriteContract, RewriteTarget
from src.code_rewriter.tools.ast_analyzer import analyze_source
from src.code_rewriter.tools.dependency_graph import (
    build_dependency_graph,
    format_dependency_slice_markdown,
    slice_dependency_graph,
)


def test_build_dependency_graph_basic():
    source = """
import os
from utils import helper

class OrderService:
    def process_order(self, order_id):
        data = self.fetch_order(order_id)
        helper(data)
        return data

    def fetch_order(self, order_id):
        cursor.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
        return cursor.fetchone()

def global_cleanup():
    print("cleanup")
"""
    analysis = analyze_source(source, "order_service.py")
    graph = build_dependency_graph(analysis, source_code=source)

    # Check node existence
    assert "order_service.py::OrderService" in graph.nodes
    assert "order_service.py::OrderService.process_order" in graph.nodes
    assert "order_service.py::OrderService.fetch_order" in graph.nodes
    assert "order_service.py::global_cleanup" in graph.nodes
    assert "order_service.py::import::os" in graph.nodes
    assert "order_service.py::import::helper" in graph.nodes

    # Check kinds
    assert graph.nodes["order_service.py::OrderService"].kind == NodeKind.CLASS
    assert graph.nodes["order_service.py::OrderService.process_order"].kind == NodeKind.METHOD
    assert graph.nodes["order_service.py::global_cleanup"].kind == NodeKind.FUNCTION

    # Check edges
    edges_from_process = [e for e in graph.edges if e.source_id == "order_service.py::OrderService.process_order"]
    target_ids = [e.target_id for e in edges_from_process]
    assert "order_service.py::OrderService.fetch_order" in target_ids
    assert "order_service.py::import::helper" in target_ids

    # Check database operation node
    db_ops = [n for n in graph.nodes.values() if n.kind == NodeKind.DATABASE_OPERATION]
    assert len(db_ops) == 2  # execute and fetchone


def test_slice_dependency_graph_cycle_handling():
    # Construct a cycle A -> B -> A
    graph = DependencyGraph()
    node_a = DependencyNode(id="mod.py::func_a", name="func_a", kind=NodeKind.FUNCTION, file_path="mod.py")
    node_b = DependencyNode(id="mod.py::func_b", name="func_b", kind=NodeKind.FUNCTION, file_path="mod.py")
    graph.add_node(node_a)
    graph.add_node(node_b)

    graph.add_edge(DependencyEdge(edge_id="e1", source_id="mod.py::func_a", target_id="mod.py::func_b", kind=DependencyType.CALL))
    graph.add_edge(DependencyEdge(edge_id="e2", source_id="mod.py::func_b", target_id="mod.py::func_a", kind=DependencyType.CALL))

    slice_res = slice_dependency_graph(graph, "func_a", max_depth=5, max_nodes=10)
    assert slice_res.target_id == "mod.py::func_a"
    assert len(slice_res.sliced_nodes) == 2
    assert "mod.py::func_a" in slice_res.sliced_nodes
    assert "mod.py::func_b" in slice_res.sliced_nodes
    assert slice_res.is_truncated is False


def test_slice_dependency_graph_depth_and_node_bounding():
    # Linear chain: n0 -> n1 -> n2 -> n3 -> n4
    graph = DependencyGraph()
    for i in range(5):
        node = DependencyNode(id=f"mod.py::f{i}", name=f"f{i}", kind=NodeKind.FUNCTION, file_path="mod.py")
        graph.add_node(node)
        if i > 0:
            graph.add_edge(DependencyEdge(edge_id=f"e{i}", source_id=f"mod.py::f{i-1}", target_id=f"mod.py::f{i}", kind=DependencyType.CALL))

    # Test max_depth = 1 from f0 -> should only reach f0 and f1
    s_depth1 = slice_dependency_graph(graph, "mod.py::f0", max_depth=1, max_nodes=10)
    assert set(s_depth1.sliced_nodes.keys()) == {"mod.py::f0", "mod.py::f1"}
    assert s_depth1.is_truncated is False

    # Test max_nodes = 2 with max_depth = 5
    s_bounded = slice_dependency_graph(graph, "mod.py::f0", max_depth=5, max_nodes=2)
    assert len(s_bounded.sliced_nodes) == 2
    assert s_bounded.is_truncated is True


def test_slice_target_not_found():
    graph = DependencyGraph()
    with pytest.raises(ValueError, match="not found in dependency graph"):
        slice_dependency_graph(graph, "non_existent")


def test_format_dependency_slice_markdown():
    source = """
class DeliveryService:
    def deliver(self, package_id):
        cursor.execute("UPDATE packages SET status = 'DELIVERED' WHERE id = %s", (package_id,))
"""
    analysis = analyze_source(source, "delivery.py")
    graph = build_dependency_graph(analysis, source_code=source)
    slice_data = slice_dependency_graph(graph, "DeliveryService.deliver")

    contract = RewriteContract(
        rewrite_id="contract_test_1",
        target=RewriteTarget(file="delivery.py", qualified_function="DeliveryService.deliver"),
        pattern="Single Update",
        strategy="Batch Update",
        must_preserve=["Package delivery state update"],
        must_not_change=["Database schema"],
    )

    md = format_dependency_slice_markdown(slice_data, contract=contract)

    assert "# Dependency Slice: `deliver`" in md
    assert "## Target Definition" in md
    assert "## Enclosing Class Context" in md
    assert "DeliveryService" in md
    assert "## Database Operations (1)" in md
    assert "UPDATE packages SET status" in md
    assert "## Optimization Contract" in md
    assert "Batch Update" in md
    assert "Package delivery state update" in md


def test_format_markdown_with_loop_flag():
    source = """
class OrderService:
    def process_orders(self, order_ids):
        cursor = self.db.cursor()
        for oid in order_ids:
            cursor.execute("SELECT * FROM orders WHERE id = %s", (oid,))
"""
    analysis = analyze_source(source, "order_service.py")
    graph = build_dependency_graph(analysis, source_code=source)
    slice_data = slice_dependency_graph(graph, "OrderService.process_orders")

    md = format_dependency_slice_markdown(slice_data)
    assert "LOOP / N+1 RISK" in md
    assert "SELECT * FROM orders WHERE id = %s" in md

