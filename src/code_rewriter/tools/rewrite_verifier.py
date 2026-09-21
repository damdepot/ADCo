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
)
from .ast_analyzer import analyze_source
from ..models.ast_models import FileAnalysis, FunctionAnalysis

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
    # The contract target is e.g. target.name.
    # Actually, a contract target might not be just a single function, but usually is.
    # I'll check all functions that match target.name, or all functions if target is file.
    
    # Let's extract functions to easily look them up by qualified_name
    orig_funcs = {f.qualified_name: f for f in _get_all_functions(orig_ast)}
    opt_funcs = {f.qualified_name: f for f in _get_all_functions(opt_ast)}

    target_name = contract.target.qualified_function or contract.target.function
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
    pattern_upper = (contract.pattern or "").upper()
    strategy_upper = (contract.strategy or "").upper()
    is_n1 = "N+1" in pattern_upper or "N_PLUS_ONE" in pattern_upper or "N_PLUS_ONE" in strategy_upper or "BATCH" in strategy_upper or "COMBINING" in strategy_upper
    if is_n1:
        # Check if original had DB ops in loops
        orig_loops = [op for op in orig_ast.database_operations if op.inside_loop]
        if orig_loops:
            # Check if optimized still has DB ops in loops (in target)
            opt_loops = [op for op in opt_ast.database_operations if op.inside_loop]
            if opt_loops:
                n1_status = "FAIL"
                n1_details = "N+1 strategy not applied"
                violations.append(VerificationViolation(
                    code="STRATEGY_NOT_APPLIED",
                    severity="ERROR",
                    message="Database operations remain inside loops"
                ))
            else:
                # Check for replacement op
                if not opt_ast.database_operations:
                    n1_status = "FAIL"
                    n1_details = "Replacement DB operation missing"
                    violations.append(VerificationViolation(
                        code="REPLACEMENT_OP_MISSING",
                        severity="ERROR",
                        message="No replacement database operations found outside loop"
                    ))
    checks.append(VerificationCheck(name="check_n_plus_one_strategy", status=n1_status, details=n1_details))

    coverage_result = check_rewrite_coverage(orig_ast, opt_ast, contract)
    violations.extend(coverage_result.violations)
    checks.extend(coverage_result.checks)

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
def check_rewrite_coverage(orig_analysis: FileAnalysis, opt_analysis: FileAnalysis, contract: RewriteContract) -> VerificationResult:
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

    pattern_upper = (contract.pattern or "").upper()
    strategy_upper = (contract.strategy or "").upper()
    is_n1 = "N+1" in pattern_upper or "N_PLUS_ONE" in pattern_upper or "N_PLUS_ONE" in strategy_upper or "BATCH" in strategy_upper or "COMBINING" in strategy_upper

    for t in contract.targets:
        fn_key = t.qualified_function or t.function
        if not fn_key:
            continue
            
        orig_f = orig_funcs.get(fn_key)
        opt_f = opt_funcs.get(fn_key)
        
        if not orig_f or not opt_f or not is_n1:
            target_coverage.append(TargetStatus(
                file=t.file,
                function=fn_key,
                status="UNVERIFIABLE",
                details="Target not found or not N+1 strategy."
            ))
            continue
            
        orig_loops = [op for op in orig_f.database_operations if op.inside_loop]
        opt_loops = [op for op in opt_f.database_operations if op.inside_loop]
        opt_non_loops = [op for op in opt_f.database_operations if not op.inside_loop]
        
        if opt_loops:
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
        elif not opt_loops and opt_non_loops:
            target_coverage.append(TargetStatus(
                file=t.file,
                function=fn_key,
                status="TRANSFORMED",
                details="N+1 loop DB operations eliminated and replaced."
            ))
        else:
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
