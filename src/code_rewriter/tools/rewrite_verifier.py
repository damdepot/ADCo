import ast
from typing import Any, Dict, List, Optional
import json

from ..models import (
    RewriteContract,
    VerificationResult,
    VerificationViolation,
    VerificationCheck,
    CheckStatus,
    VerificationStatus,
    ViolationSeverity,
    TargetStatus,
    DependencyType,
)
from .ast_analyzer import analyze_source
from ..models.ast_models import FileAnalysis, FunctionAnalysis
from .dependency_graph import build_dependency_graph, DependencyGraph

def _extract_function_ast_map(source: Optional[str]) -> Dict[str, ast.AST]:
    if not source:
        return {}
    try:
        tree = ast.parse(source)
    except Exception:
        return {}
    res: Dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            res[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    res[f"{node.name}.{child.name}"] = child
                    if child.name not in res:
                        res[child.name] = child
    return res

def _is_n1(contract: RewriteContract) -> bool:
    """True when the contract targets an N+1 / batching / combining rewrite."""
    pattern_upper = (contract.pattern or "").upper()
    strategy_upper = (contract.strategy or "").upper()
    return "N+1" in pattern_upper or "N_PLUS_ONE" in pattern_upper or "N_PLUS_ONE" in strategy_upper or "BATCH" in strategy_upper or "COMBINING" in strategy_upper

def verify_rewrite(original_source: str, optimized_source: str, contract: RewriteContract) -> VerificationResult:
    violations: List[VerificationViolation] = []
    checks: List[VerificationCheck] = []
    
    orig_ast = analyze_source(original_source)
    opt_ast = analyze_source(optimized_source)

    # 1. check_syntax
    syntax_status: CheckStatus = "PASS"
    syntax_details = "Both files parsed successfully"
    if not orig_ast.parse_success:
        syntax_status = "FAIL"
        syntax_details = "Original file has syntax errors"
        violations.append(VerificationViolation(
            code="ORIGINAL_SYNTAX_ERROR",
            severity="ERROR",
            message=str(orig_ast.parse_error)
        ))
    if not opt_ast.parse_success:
        syntax_status = "FAIL"
        syntax_details = "Optimized file has syntax errors"
        violations.append(VerificationViolation(
            code="SYNTAX_ERROR",
            severity="ERROR",
            message=str(opt_ast.parse_error)
        ))
    checks.append(VerificationCheck(name="check_syntax", status=syntax_status, details=syntax_details))
    
    if syntax_status == "FAIL":
        # If syntax fails, we can't reliably do the rest
        return VerificationResult(
            status="FAIL",
            violations=violations,
            checks=checks,
            summary="Verification failed due to syntax errors."
        )

    # Get target function(s)
    orig_funcs = {f.qualified_name: f for f in _get_all_functions(orig_ast)}
    opt_funcs = {f.qualified_name: f for f in _get_all_functions(opt_ast)}

    orig_ast_map = _extract_function_ast_map(original_source)
    opt_ast_map = _extract_function_ast_map(optimized_source)

    target_name = contract.target.qualified_function or contract.target.function
    target_file = contract.target.file or ""

    # 2. check_target_exists
    target_exists_status: CheckStatus = "PASS"
    target_exists_details = f"Target {target_name} found in optimized code"
    if target_name:
        found_target = any(f.name == target_name or f.qualified_name == target_name or f.qualified_name.endswith("." + target_name) for f in opt_funcs.values())
        if not found_target:
            target_exists_status = "FAIL"
            target_exists_details = f"Target {target_name} missing from optimized code"
            violations.append(VerificationViolation(
                code="TARGET_MISSING",
                severity="ERROR",
                message=f"Target {target_name} is missing in optimized AST",
                expected=target_name,
                actual=None
            ))
        elif not contract.targets:
            # Check single target transformation if targets list is empty
            orig_node = orig_ast_map.get(target_name)
            opt_node = opt_ast_map.get(target_name)
            if orig_node is not None and opt_node is not None and ast.dump(orig_node) == ast.dump(opt_node):
                violations.append(VerificationViolation(
                    code="MISSING_REWRITE",
                    severity="ERROR",
                    message=f"Target function '{target_name}' in '{target_file}' has not been transformed (AST is unchanged from original)"
                ))
    checks.append(VerificationCheck(name="check_target_exists", status=target_exists_status, details=target_exists_details))

    # 3. check_function_signature & 4. check_allowed_regions & 5. check_return_behavior
    sig_status: CheckStatus = "PASS"
    sig_details = "Signatures match"
    
    allowed_status: CheckStatus = "PASS"
    allowed_details = "No unauthorized changes detected"
    
    return_status: CheckStatus = "PASS"
    return_details = "Return behavior preserved"

    for qname, orig_f in orig_funcs.items():
        opt_f = opt_funcs.get(qname)
        if not opt_f:
            continue
            
        is_target = target_name and (orig_f.name == target_name or orig_f.qualified_name == target_name or orig_f.qualified_name.endswith("." + target_name))
        is_in_allowed = is_target or qname in contract.allowed_regions or orig_f.name in contract.allowed_regions
        
        # Check signature changes
        if orig_f.parameters != opt_f.parameters:
            if is_in_allowed:
                # Target signature changed, check if it's allowed
                sig_status = "FAIL"
                sig_details = "Function signature changed"
                violations.append(VerificationViolation(
                    code="FUNCTION_SIGNATURE_CHANGED",
                    severity="ERROR",
                    message=f"Signature changed for {qname}",
                    expected=orig_f.parameters,
                    actual=opt_f.parameters
                ))
            else:
                allowed_status = "FAIL"
                allowed_details = "Unauthorized function changed"
                violations.append(VerificationViolation(
                    code="UNAUTHORIZED_CHANGE",
                    severity="ERROR",
                    message=f"Unauthorized change to signature of {qname}",
                    expected=orig_f.parameters,
                    actual=opt_f.parameters
                ))

        # Check return behavior
        must_preserve_return = "return_type" in contract.must_preserve or "function_signature" in contract.must_preserve
        if must_preserve_return:
            if orig_f.return_count > 0 and opt_f.return_count == 0:
                return_status = "FAIL"
                return_details = "Return statements removed"
                violations.append(VerificationViolation(
                    code="RETURN_BEHAVIOR_CHANGED",
                    severity="ERROR",
                    message=f"All return statements lost in {qname}",
                    expected=orig_f.return_count,
                    actual=0
                ))

    checks.append(VerificationCheck(name="check_function_signature", status=sig_status, details=sig_details))
    checks.append(VerificationCheck(name="check_allowed_regions", status=allowed_status, details=allowed_details))
    checks.append(VerificationCheck(name="check_return_behavior", status=return_status, details=return_details))

    # 6. check_db_operations
    db_op_status: CheckStatus = "PASS"
    db_op_details = "Database operations consistent"
    must_preserve_db = "transaction_semantics" in contract.must_preserve or "exception_behavior" in contract.must_preserve
    if must_preserve_db:
        if orig_ast.database_operations and not opt_ast.database_operations:
            db_op_status = "FAIL"
            db_op_details = "All DB operations removed"
            violations.append(VerificationViolation(
                code="DB_OPERATION_REMOVED",
                severity="ERROR",
                message="All database operations were removed despite preservation contract"
            ))
    checks.append(VerificationCheck(name="check_db_operations", status=db_op_status, details=db_op_details))

    # 7. check_n_plus_one_strategy
    n1_status: CheckStatus = "PASS"
    n1_details = "Strategy requirements met"
    is_n1 = _is_n1(contract)

    target_regions = set(contract.allowed_regions or [])
    if contract.target:
        target_regions.add(contract.target.qualified_function or contract.target.function)
    for t in (contract.targets or []):
        if t.qualified_function:
            target_regions.add(t.qualified_function)
        if t.function:
            target_regions.add(t.function)

    if is_n1 and target_regions:
        target_orig_funcs = [f for f in _get_all_functions(orig_ast) if (f.qualified_name in target_regions or f.name in target_regions)]
        target_opt_funcs = [f for f in _get_all_functions(opt_ast) if (f.qualified_name in target_regions or f.name in target_regions)]

        orig_target_loops = [op for f in target_orig_funcs for op in f.database_operations if op.inside_loop]
        opt_target_loops = [op for f in target_opt_funcs for op in f.database_operations if op.inside_loop]
        opt_target_all = [op for f in target_opt_funcs for op in f.database_operations]

        if orig_target_loops:
            if opt_target_loops:
                residual_fns = sorted(set(
                    op.containing_function for op in opt_target_loops if op.containing_function
                ))
                n1_status = "FAIL"
                n1_details = (
                    f"Strict-zero not met: {len(opt_target_loops)} DB op(s) remain inside loops "
                    f"(was {len(orig_target_loops)}, required 0). "
                    f"Functions with residual loops: {residual_fns}"
                )
                violations.append(VerificationViolation(
                    code="STRATEGY_NOT_APPLIED",
                    severity="ERROR",
                    message=(
                        f"Strict-zero violation: {len(opt_target_loops)} DB op(s) still inside loops "
                        f"in {residual_fns} (required 0). "
                        "Hoist all reads before loop with batch IN query; remove all cursor.execute from loop body."
                    )
                ))
            elif not opt_target_all:
                n1_status = "FAIL"
                n1_details = "Replacement DB operation missing"
                violations.append(VerificationViolation(
                    code="REPLACEMENT_OP_MISSING",
                    severity="ERROR",
                    message="No replacement database operations found outside loop"
                ))
    checks.append(VerificationCheck(name="check_n_plus_one_strategy", status=n1_status, details=n1_details))

    coverage_result = check_rewrite_coverage(orig_ast, opt_ast, contract, orig_source=original_source, opt_source=optimized_source)
    violations.extend(coverage_result.violations)
    checks.extend(coverage_result.checks)

    # Dependency integrity check
    orig_graph = build_dependency_graph(orig_ast, original_source)
    opt_graph = build_dependency_graph(opt_ast, optimized_source)
    dep_violations = check_dependency_integrity(orig_graph, opt_graph, contract)
    violations.extend(dep_violations)
    checks.append(VerificationCheck(
        name="check_dependency_integrity",
        status="PASS" if not dep_violations else "FAIL",
        details=f"{len(dep_violations)} dependency violations"
    ))

    overall_status: VerificationStatus = "PASS" if not violations else "FAIL"
    summary = f"Verification {'passed' if overall_status == 'PASS' else 'failed'} with {len(violations)} violations."
    
    return VerificationResult(
        status=overall_status,
        violations=violations,
        checks=checks,
        summary=summary,
        target_coverage=coverage_result.target_coverage,
        expected_targets=coverage_result.expected_targets,
        transformed_targets=coverage_result.transformed_targets,
        missing_targets=coverage_result.missing_targets,
        rewrite_coverage=coverage_result.rewrite_coverage
    )

def _get_all_functions(ast: FileAnalysis) -> List[FunctionAnalysis]:
    funcs = list(ast.functions)
    for cls in ast.classes:
        funcs.extend(cls.methods)
    return funcs

def check_rewrite_coverage(
    orig_analysis: FileAnalysis, 
    opt_analysis: FileAnalysis, 
    contract: RewriteContract,
    orig_source: Optional[str] = None,
    opt_source: Optional[str] = None,
) -> VerificationResult:
    target_coverage: List[TargetStatus] = []
    checks: List[VerificationCheck] = []
    violations: List[VerificationViolation] = []

    if not contract.targets:
        checks.append(VerificationCheck(
            name="check_rewrite_coverage", 
            status="PASS", 
            details="No multi-target list in contract."
        ))
        return VerificationResult(
            status="PASS",
            summary="Coverage check completed",
            checks=checks,
            violations=violations,
            target_coverage=[],
            expected_targets=0,
            transformed_targets=0,
            missing_targets=0,
            rewrite_coverage=1.0
        )

    orig_funcs = {f.qualified_name: f for f in _get_all_functions(orig_analysis)}
    orig_funcs.update({f.name: f for f in _get_all_functions(orig_analysis) if f.name not in orig_funcs})
    
    opt_funcs = {f.qualified_name: f for f in _get_all_functions(opt_analysis)}
    opt_funcs.update({f.name: f for f in _get_all_functions(opt_analysis) if f.name not in opt_funcs})

    orig_ast_map = _extract_function_ast_map(orig_source) if orig_source else {}
    opt_ast_map = _extract_function_ast_map(opt_source) if opt_source else {}

    is_n1 = _is_n1(contract)

    for t in contract.targets:
        fn_key = t.qualified_function or t.function
        if not fn_key:
            continue
            
        orig_f = orig_funcs.get(fn_key)
        opt_f = opt_funcs.get(fn_key)
        orig_node = orig_ast_map.get(fn_key)
        opt_node = opt_ast_map.get(fn_key)
        
        if not orig_f or not opt_f:
            target_coverage.append(TargetStatus(
                file=t.file,
                function=fn_key,
                status="MISSING_REWRITE" if not opt_f else "UNVERIFIABLE",
                details=f"Target function '{fn_key}' in '{t.file}' {'missing from optimized code' if not opt_f else 'not found in original code'}."
            ))
            if not opt_f:
                violations.append(VerificationViolation(
                    code="MISSING_REWRITE",
                    severity="ERROR",
                    message=f"Target function '{fn_key}' in '{t.file}' missing in optimized code"
                ))
            continue
            
        # Check if AST is unchanged
        if orig_node is not None and opt_node is not None and ast.dump(orig_node) == ast.dump(opt_node):
            target_coverage.append(TargetStatus(
                file=t.file,
                function=fn_key,
                status="MISSING_REWRITE",
                details=f"Expected optimization target '{fn_key}' in '{t.file}' remains structurally unchanged"
            ))
            violations.append(VerificationViolation(
                code="MISSING_REWRITE",
                severity="ERROR",
                message=f"Expected optimization target '{fn_key}' in '{t.file}' remains structurally unchanged"
            ))
            continue

        orig_loops = [op for op in orig_f.database_operations if op.inside_loop]
        opt_loops = [op for op in opt_f.database_operations if op.inside_loop]
        opt_non_loops = [op for op in opt_f.database_operations if not op.inside_loop]
        
        if is_n1 and orig_loops and opt_loops:
            target_coverage.append(TargetStatus(
                file=t.file,
                function=fn_key,
                status="MISSING_REWRITE",
                details=(
                    f"'{fn_key}' in '{t.file}': {len(opt_loops)} loop DB op(s) remain "
                    f"(was {len(orig_loops)}, required 0). "
                    "If the loop fetches by 2+ columns, apply composite-key batch (Pattern 6)."
                )
            ))
            violations.append(VerificationViolation(
                code="MISSING_REWRITE",
                severity="ERROR",
                message=(
                    f"'{fn_key}': {len(opt_loops)}/{len(orig_loops)} loop ops remain "
                    f"(strict zero required). Hoist batch query before loop; remove all "
                    f"cursor.execute from loop body."
                )
            ))
        elif is_n1 and orig_loops and not opt_loops and not opt_non_loops:
            target_coverage.append(TargetStatus(
                file=t.file,
                function=fn_key,
                status="INVALID_REWRITE",
                details="N+1 loop DB operations eliminated but no replacement found."
            ))
            violations.append(VerificationViolation(
                code="INVALID_REWRITE",
                severity="ERROR",
                message=f"No replacement database operations found outside loop in target '{fn_key}'"
            ))
        else:
            target_coverage.append(TargetStatus(
                file=t.file,
                function=fn_key,
                status="TRANSFORMED",
                details="Target function transformed."
            ))
            
    expected_targets = len(contract.targets)
    transformed_targets = sum(1 for tc in target_coverage if tc.status == "TRANSFORMED")
    missing_targets = expected_targets - transformed_targets
    rewrite_coverage = round(transformed_targets / expected_targets, 3) if expected_targets > 0 else 1.0
    
    check_status = "PASS" if missing_targets == 0 else "FAIL"
    checks.append(VerificationCheck(
        name="check_rewrite_coverage",
        status=check_status,
        details=f"{transformed_targets}/{expected_targets} targets transformed (coverage={rewrite_coverage})"
    ))
    
    overall_status = "PASS" if not violations else "FAIL"

    return VerificationResult(
        status=overall_status,
        summary=f"Coverage check completed with {len(violations)} violations",
        checks=checks,
        violations=violations,
        target_coverage=target_coverage,
        expected_targets=expected_targets,
        transformed_targets=transformed_targets,
        missing_targets=missing_targets,
        rewrite_coverage=rewrite_coverage
    )

def check_dependency_integrity(original_graph: DependencyGraph, optimized_graph: DependencyGraph, contract: RewriteContract) -> List[VerificationViolation]:
    violations: List[VerificationViolation] = []
    
    targets_to_check = contract.targets if contract.targets else ([contract.target] if contract.target else [])
    target_names = {t.qualified_function or t.function for t in targets_to_check if t.qualified_function or t.function}
    
    orig_nodes = original_graph.nodes
    opt_nodes = optimized_graph.nodes
    orig_edges = original_graph.edges
    opt_edges = optimized_graph.edges
    
    opt_node_ids = set(opt_nodes.keys())
    orig_node_ids = set(orig_nodes.keys())
    
    # 1. External callers outside targets must not be removed
    for edge in orig_edges:
        if edge.target_id in target_names and edge.kind == DependencyType.CALL:
            caller_id = edge.source_id
            if caller_id not in target_names:
                if caller_id not in opt_node_ids:
                    violations.append(VerificationViolation(
                        code="CALLER_INTEGRITY_VIOLATION",
                        severity="ERROR",
                        message=f"Caller {caller_id} is missing in optimized code"
                    ))
                else:
                    has_call = any(e.source_id == caller_id and e.target_id == edge.target_id and e.kind == DependencyType.CALL for e in opt_edges)
                    if not has_call:
                        violations.append(VerificationViolation(
                            code="CALLER_INTEGRITY_VIOLATION",
                            severity="ERROR",
                            message=f"Call from {caller_id} to {edge.target_id} is missing in optimized code"
                        ))
                        
    # 2. Invariant class state outside targets must not be removed
    for edge in orig_edges:
        if edge.kind == DependencyType.CLASS_STATE:
            # Only check class state for non-target functions
            if edge.source_id not in target_names and not any(t in edge.source_id for t in target_names):
                has_edge = any(e.source_id == edge.source_id and e.target_id == edge.target_id and e.kind == DependencyType.CLASS_STATE for e in opt_edges)
                if not has_edge:
                    violations.append(VerificationViolation(
                        code="CLASS_STATE_CORRUPTED",
                        severity="ERROR",
                        message=f"Class state edge {edge.source_id} -> {edge.target_id} corrupted"
                    ))
                    
    # 3. New dependencies in targets
    AUTHORIZED_BUILTINS = {"len", "range", "dict", "list", "set", "str", "int", "float", "bool", "min", "max", "sum", "zip", "enumerate"}
    AUTHORIZED_METHODS = {"join", "append", "extend", "update", "get", "add", "execute", "executemany", "fetchall", "fetchone", "commit", "rollback", "close", "format", "strip", "split"}
    AUTHORIZED_IMPORTS = {"psycopg2", "sqlite3", "mysql", "collections", "itertools", "typing", "json", "re", "os", "sys"}
    
    for t_name in target_names:
        matched_opt_ids = [nid for nid in opt_node_ids if t_name in nid or nid.endswith(t_name)]
        if not matched_opt_ids:
            continue
            
        for opt_t_id in matched_opt_ids:
            opt_out_edges = [e for e in opt_edges if e.source_id == opt_t_id]
            orig_out_edges = [e for e in orig_edges if e.source_id == opt_t_id or t_name in e.source_id]
            orig_out_targets = {e.target_id for e in orig_out_edges}
            
            for edge in opt_out_edges:
                tid = edge.target_id
                if tid in orig_out_targets or tid in orig_node_ids or edge.kind == DependencyType.DATABASE_OPERATION:
                    continue
                    
                tid_base = tid.split('.')[0] if '.' in tid else tid
                tid_method = tid.split('.')[-1] if '.' in tid else tid
                
                is_auth = (tid in AUTHORIZED_BUILTINS) or (tid_method in AUTHORIZED_METHODS) or (tid_base in AUTHORIZED_IMPORTS)
                
                if not is_auth:
                    violations.append(VerificationViolation(
                        code="UNAUTHORIZED_DEPENDENCY",
                        severity="ERROR",
                        message=f"Unauthorized dependency {tid} introduced in {t_name}"
                    ))
                    
    return violations
