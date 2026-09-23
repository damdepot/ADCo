"""Advisory transformation-risk reporting for database-interaction rewrites.

This module compares the :class:`FunctionDbModel` of a function *before* and
*after* a rewrite and emits structural risk flags plus a conservative risk
level.  It is deliberately **schema-agnostic** (no hardcoded table, column or
function names) and purely **advisory**: nothing here ever rejects or fails a
rewrite.  It is a structural risk *proxy*, not a performance predictor.

Flags
-----
``DEPENDENT_QUERY_FUSION``
    The original function had at least one linear (non-loop) value dependency
    and the rewritten function has fewer cursor executions.  A data dependency
    between statements was therefore fused away.
``JOIN_COMPLEXITY_INCREASE``
    The maximum number of top-level relations across the rewritten statements
    exceeds the same maximum across the original statements.
``AGGREGATE_QUERY_EXPANSION``
    The maximum number of top-level relations across statements that contain an
    aggregate grew from the original to the rewrite.
``CROSS_FUNCTION_READ_WRITE``
    The rewrite reads a table that the broader codebase writes from another
    function.
``STATEMENT_ORDER_CHANGE``
    The ordered sequence of SQL operations changed while the operation multiset
    stayed the same (a conservative, low-noise signal; ambiguous cases omit it).

Risk rule
---------
``HIGH``
    fusion **and** (join increase **or** aggregate expansion) **and** a
    cross-function written table.
``MEDIUM``
    a fused linear dependency on its own, or a statement-order change.  Fusing a
    dependency is conservatively treated as medium risk even when the query does
    not visibly grow, because it removes an intermediate materialization that is
    no longer independently observable.
``LOW``
    everything else.

The rule is intentionally conservative: it prefers false positives over false
negatives, since this is an advisory signal only.
"""

from __future__ import annotations

import ast
import os
from typing import Any, Dict, List, Optional, Tuple

from ..models.db_interaction_models import FunctionDbModel
from ..models.transformation_risk_models import RiskReport
from .db_interaction import build_function_model
from .sql_resolver import collect_module_dicts
from .._common import find_function_node

DEPENDENT_QUERY_FUSION = "DEPENDENT_QUERY_FUSION"
JOIN_COMPLEXITY_INCREASE = "JOIN_COMPLEXITY_INCREASE"
AGGREGATE_QUERY_EXPANSION = "AGGREGATE_QUERY_EXPANSION"
CROSS_FUNCTION_READ_WRITE = "CROSS_FUNCTION_READ_WRITE"
STATEMENT_ORDER_CHANGE = "STATEMENT_ORDER_CHANGE"


def model_from_source(source: str, function_name: str) -> Optional[FunctionDbModel]:
    """Build a :class:`FunctionDbModel` for *function_name* inside *source*.

    Returns ``None`` when *source* is empty/unparseable or the function cannot be
    found.  The function may be referenced by its qualified or bare name.
    """
    if not isinstance(source, str) or not source or not function_name:
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    module_dicts = collect_module_dicts(tree)
    node = find_function_node(tree, function_name)
    if node is None:
        return None

    try:
        return build_function_model(
            node, module_dicts, file="", function=function_name
        )
    except Exception:
        return None


def _max_top_level_relations(model: FunctionDbModel) -> int:
    values = [
        stmt.model.top_level_relations
        for stmt in model.statements
        if stmt.model is not None
    ]
    return max(values) if values else 0


def _max_aggregate_relations(model: FunctionDbModel) -> int:
    values = [
        stmt.model.top_level_relations
        for stmt in model.statements
        if stmt.model is not None and stmt.model.has_aggregate
    ]
    return max(values) if values else 0


def _cross_function_tables(
    opt: FunctionDbModel, read_write_map: Dict, function: str
) -> List[Tuple[str, str]]:
    """Return ``(table, writer)`` pairs read by *opt* and written elsewhere."""
    hits: List[Tuple[str, str]] = []
    if not isinstance(read_write_map, dict):
        return hits

    seen: set[str] = set()
    for stmt in opt.statements:
        if stmt.model is None:
            continue
        for table in stmt.model.tables_read:
            if table in seen:
                continue
            entry = read_write_map.get(table)
            if not isinstance(entry, dict):
                continue
            writers = entry.get("written_by") or []
            others = [w for w in writers if w and w != function]
            if others:
                seen.add(table)
                hits.append((table, others[0]))
    return hits


def _statement_order_changed(orig: FunctionDbModel, opt: FunctionDbModel) -> bool:
    """True when only the ordering of SQL operations changed (not the multiset)."""
    before = [stmt.sql_operation for stmt in orig.statements]
    after = [stmt.sql_operation for stmt in opt.statements]
    if not before or not after or before == after:
        return False
    return sorted(before) == sorted(after)


def _classify_risk(
    fusion: bool, join_increase: bool, aggregate_expansion: bool, cross: bool, order_change: bool
) -> str:
    expansion = join_increase or aggregate_expansion
    if fusion and expansion and cross:
        return "HIGH"
    if fusion or order_change:
        return "MEDIUM"
    return "LOW"


def _risk_evidence(
    risk: str, fusion: bool, expansion: bool, cross: bool
) -> str:
    reasons: List[str] = []
    if fusion:
        reasons.append("dependent fusion")
    if expansion:
        reasons.append("relation expansion")
    if cross:
        reasons.append("cross-function written table")
    suffix = " + ".join(reasons) if reasons else "no structural escalation"
    return f"risk {risk}: {suffix}"


def compare_function_models(
    orig: FunctionDbModel,
    opt: FunctionDbModel,
    read_write_map: Dict,
    function: str = "",
) -> RiskReport:
    """Compare *orig* and *opt* models and return a conservative :class:`RiskReport`."""
    name = function or orig.function or opt.function or ""

    statements_before = len(orig.statements)
    statements_after = len(opt.statements)
    max_relations_before = _max_top_level_relations(orig)
    max_relations_after = _max_top_level_relations(opt)
    max_aggregate_before = _max_aggregate_relations(orig)
    max_aggregate_after = _max_aggregate_relations(opt)

    fusion = bool(orig.value_edges) and statements_after < statements_before
    join_increase = max_relations_after > max_relations_before
    aggregate_expansion = max_aggregate_after > max_aggregate_before
    cross_tables = _cross_function_tables(opt, read_write_map, name)
    order_change = _statement_order_changed(orig, opt)

    flags: List[str] = []
    evidence: List[str] = []

    if fusion:
        flags.append(DEPENDENT_QUERY_FUSION)
        for producer, consumer in orig.value_edges:
            evidence.append(
                f"S{producer + 1} -> S{consumer + 1} linear value dependency was fused"
            )
        evidence.append(
            f"statement count {statements_before} -> {statements_after}"
        )

    if join_increase:
        flags.append(JOIN_COMPLEXITY_INCREASE)
        evidence.append(
            f"max top-level relations {max_relations_before} -> {max_relations_after}"
        )

    if aggregate_expansion:
        flags.append(AGGREGATE_QUERY_EXPANSION)
        evidence.append(
            "aggregate query max top-level relations "
            f"{max_aggregate_before} -> {max_aggregate_after}"
        )

    if cross_tables:
        flags.append(CROSS_FUNCTION_READ_WRITE)
        for table, writer in cross_tables:
            evidence.append(f"reads {table} which is written by {writer}")

    if order_change:
        flags.append(STATEMENT_ORDER_CHANGE)
        evidence.append(
            "statement operation order changed while the operation multiset is unchanged"
        )

    risk = _classify_risk(
        fusion, join_increase, aggregate_expansion, bool(cross_tables), order_change
    )
    evidence.append(
        _risk_evidence(
            risk,
            fusion,
            join_increase or aggregate_expansion,
            bool(cross_tables),
        )
    )

    return RiskReport(
        function=name,
        risk=risk,
        flags=flags,
        evidence=evidence,
        statements_before=statements_before,
        statements_after=statements_after,
        max_relations_before=max_relations_before,
        max_relations_after=max_relations_after,
        fused_dependencies=len(orig.value_edges) if fusion else 0,
    )


def _contract_target(contract: Any) -> Tuple[str, str, str]:
    """Return ``(file, qualified_function, function)`` from a contract or dict."""
    target = (
        contract.get("target")
        if isinstance(contract, dict)
        else getattr(contract, "target", None)
    )
    if target is None:
        return "", "", ""
    if isinstance(target, dict):
        return (
            target.get("file") or "",
            target.get("qualified_function") or "",
            target.get("function") or "",
        )
    return (
        getattr(target, "file", "") or "",
        getattr(target, "qualified_function", "") or "",
        getattr(target, "function", "") or "",
    )


def _read_text(path: str) -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return None


def analyze_candidate(
    original_source: str,
    candidate_source: str,
    function: str,
    read_write_map: Dict,
) -> Optional[RiskReport]:
    """Compare *function* between *original_source* and *candidate_source*.

    Builds a :class:`FunctionDbModel` from both sources and delegates to
    :func:`compare_function_models`.  Returns ``None`` on any missing or
    unparseable input (including when either function cannot be found).
    """
    if not function:
        return None
    try:
        orig_model = model_from_source(original_source, function)
        opt_model = model_from_source(candidate_source, function)
        if orig_model is None or opt_model is None:
            return None
        return compare_function_models(
            orig_model, opt_model, read_write_map, function=function
        )
    except Exception:
        return None


def analyze_transformation(
    target_dir: str,
    sandbox_dir: str,
    contract: Any,
    read_write_map: Dict,
) -> Optional[RiskReport]:
    """Compare the contract target file between *target_dir* and *sandbox_dir*.

    ``contract`` may be a :class:`RewriteContract` or a plain dict.  Returns
    ``None`` on any missing or unparseable input.
    """
    try:
        rel_file, qualified, function = _contract_target(contract)
        if not rel_file:
            return None
        name = qualified or function
        if not name:
            return None

        orig_source = _read_text(os.path.join(target_dir, rel_file))
        opt_source = _read_text(os.path.join(sandbox_dir, rel_file))
        if orig_source is None or opt_source is None:
            return None

        orig_model = model_from_source(orig_source, name)
        opt_model = model_from_source(opt_source, name)
        if orig_model is None or opt_model is None:
            return None

        return compare_function_models(
            orig_model, opt_model, read_write_map, function=name
        )
    except Exception:
        return None
