import os
from typing import Any

from .ast_analyzer import analyze_file
from ..models.ast_models import FileAnalysis
from ..models.rewrite_models import RewriteContract, RewriteTarget
from .rewrite_contract import build_rewrite_contract
from .rewrite_verifier import verify_rewrite, VerificationResult, TargetStatus, VerificationCheck, VerificationViolation

EXCLUDED_SETUP_FUNCTION_PATTERNS = {
    "_execute_ddl", "execute_ddl", "load_schema", "init_schema", 
    "_load_schema", "create_tables", "init_db", "_init_db", "setup_schema"
}

def is_ddl_or_setup_func(fn) -> bool:
    name_lower = fn.name.lower()
    if fn.name in EXCLUDED_SETUP_FUNCTION_PATTERNS:
        return True
    if "ddl" in name_lower or "schema" in name_lower:
        return True
    return False

def build_contracts_from_intent(
    target_dir: str, 
    intent_output: dict, 
    strategy: str = "COMBINING_QUERIES", 
    pattern: str = "N_PLUS_ONE_QUERY"
) -> tuple[dict[str, FileAnalysis], list[RewriteContract]]:
    targets = intent_output.get("optimization_targets", [])
    analyses: dict[str, FileAnalysis] = {}
    
    multi_targets = []
    
    # Analyze each file
    for target in targets:
        rel_path = target.get("file")
        if not rel_path:
            continue
        abs_path = os.path.join(target_dir, rel_path)
        if not os.path.exists(abs_path):
            continue
            
        try:
            analysis = analyze_file(abs_path)
            analyses[rel_path] = analysis
            
            # Find candidate functions
            funcs_to_check = list(analysis.functions)
            for cls in analysis.classes:
                funcs_to_check.extend(cls.methods)
                
            for fn in funcs_to_check:
                if is_ddl_or_setup_func(fn):
                    continue
                has_loop_db = any(op.inside_loop for op in fn.database_operations)
                if has_loop_db:
                    multi_targets.append(RewriteTarget(
                        file=rel_path,
                        function=fn.name,
                        qualified_function=fn.qualified_name,
                        source_location=fn.source_location
                    ))
        except Exception:
            continue
            
    contracts = []
    for rel_path, file_analysis in analyses.items():
        file_targets = [t for t in multi_targets if t.file == rel_path]
        if not file_targets:
            continue
        primary = file_targets[0]
        contract = build_rewrite_contract(
            analysis=file_analysis,
            target=primary,
            pattern=pattern,
            strategy=strategy,
        )
        contract.targets = file_targets
        contract.allowed_regions = [
            t.qualified_function or t.function
            for t in file_targets
            if (t.qualified_function or t.function)
        ]
        contracts.append(contract)
                
    return analyses, contracts

def execute_deterministic_verification(
    target_dir: str, 
    sandbox_dir: str, 
    contracts: list[RewriteContract], 
    modified_files: list[str]
) -> VerificationResult:
    if not contracts:
        return VerificationResult(
            status="PASS",
            violations=[],
            checks=[],
            summary="No contracts to verify.",
            target_coverage=[],
            expected_targets=0,
            transformed_targets=0,
            missing_targets=0,
            rewrite_coverage=1.0
        )
        
    all_contract_targets = []
    for c in contracts:
        targets_to_check = c.targets if c.targets else ([c.target] if c.target else [])
        all_contract_targets.extend(targets_to_check)
    expected_targets_count = len(all_contract_targets)
    
    if not modified_files and expected_targets_count > 0:
        violations = []
        target_coverage = []
        for t in all_contract_targets:
            target_coverage.append(TargetStatus(
                file=t.file,
                function=t.qualified_function or t.function,
                status="MISSING_REWRITE",
                details="File not modified"
            ))
            violations.append(VerificationViolation(
                code="MISSING_REWRITE",
                severity="ERROR",
                message=f"File not modified for target '{t.qualified_function or t.function}' in '{t.file}'"
            ))
        
        return VerificationResult(
            status="FAIL",
            violations=violations,
            checks=[VerificationCheck(name="check_rewrite_coverage", status="FAIL", details="No files modified.")],
            summary="Verification failed: no files modified.",
            target_coverage=target_coverage,
            expected_targets=expected_targets_count,
            transformed_targets=0,
            missing_targets=expected_targets_count,
            rewrite_coverage=0.0
        )

    all_violations = []
    all_checks = []
    all_target_coverage = []
    
    active_contracts = [c for c in contracts if c.target and c.target.file in modified_files]
    
    for c in active_contracts:
        f = c.target.file
        orig_src = os.path.join(target_dir, f)
        opt_src = os.path.join(sandbox_dir, f)
        if os.path.exists(orig_src) and os.path.exists(opt_src):
            try:
                with open(orig_src, 'r', encoding='utf-8') as file:
                    orig_code = file.read()
                with open(opt_src, 'r', encoding='utf-8') as file:
                    opt_code = file.read()
                
                result = verify_rewrite(orig_code, opt_code, c)
                all_violations.extend(result.violations)
                for check in result.checks:
                    if not any(existing.name == check.name and existing.status == check.status for existing in all_checks):
                        all_checks.append(check)
                all_target_coverage.extend(result.target_coverage)
            except Exception as e:
                all_violations.append(VerificationViolation(
                    code="VERIFICATION_ERROR",
                    severity="ERROR",
                    message=f"Error verifying {f}: {str(e)}"
                ))
    
    # Deduplicate target_coverage by file and function
    seen_targets = set()
    dedup_coverage = []
    for tc in all_target_coverage:
        key = (tc.file, tc.function)
        if key not in seen_targets:
            seen_targets.add(key)
            dedup_coverage.append(tc)
            
    # Recalculate targets based on deduplicated coverage or active contracts
    expected_targets = len(dedup_coverage) if dedup_coverage else sum(len(c.targets) if c.targets else 1 for c in active_contracts)
    transformed_targets = sum(1 for tc in dedup_coverage if tc.status == "TRANSFORMED")
    missing_targets = expected_targets - transformed_targets
    rewrite_coverage = round(transformed_targets / expected_targets, 3) if expected_targets > 0 else 1.0

    critical_codes = {
        "SYNTAX_ERROR", "ORIGINAL_SYNTAX_ERROR", "TARGET_MISSING",
        "FUNCTION_SIGNATURE_CHANGED", "UNAUTHORIZED_CHANGE",
        "DB_OPERATION_REMOVED", "INVALID_REWRITE"
    }
    has_critical = any(v.severity == "ERROR" and v.code in critical_codes for v in all_violations)

    if has_critical or transformed_targets == 0:
        overall_status = "FAIL"
    elif transformed_targets > 0:
        overall_status = "PASS"
    else:
        overall_status = "PASS" if not all_violations else "FAIL"

    return VerificationResult(
        status=overall_status,
        violations=all_violations,
        checks=all_checks,
        summary=f"Verification {'passed' if overall_status == 'PASS' else 'failed'} with {len(all_violations)} violations.",
        target_coverage=dedup_coverage,
        expected_targets=expected_targets,
        transformed_targets=transformed_targets,
        missing_targets=missing_targets,
        rewrite_coverage=rewrite_coverage
    )

def serialize_verification_results(result: VerificationResult) -> dict:
    return result.model_dump()
