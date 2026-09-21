import os
from typing import Any

from .ast_analyzer import analyze_file
from ..models.ast_models import FileAnalysis
from ..models.rewrite_models import RewriteContract, RewriteTarget
from .rewrite_contract import build_rewrite_contract
from .rewrite_verifier import verify_rewrite, VerificationResult, TargetStatus, VerificationCheck, VerificationViolation

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
    if multi_targets:
        primary = multi_targets[0]
        analysis = analyses.get(primary.file)
        if analysis:
            contract = build_rewrite_contract(
                analysis=analysis,
                target=primary,
                pattern=pattern,
                strategy=strategy,
            )
            contract.targets = multi_targets
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
        
    contract = contracts[0]
    expected_targets = len(contract.targets) if contract.targets else (1 if contract.target else 0)
    
    if not modified_files and expected_targets > 0:
        violations = []
        target_coverage = []
        targets_to_check = contract.targets if contract.targets else [contract.target]
        for t in targets_to_check:
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
            expected_targets=expected_targets,
            transformed_targets=0,
            missing_targets=expected_targets,
            rewrite_coverage=0.0
        )

    all_violations = []
    all_checks = []
    all_target_coverage = []
    expected = 0
    transformed = 0
    missing = 0
    
    for c in contracts:
        files_to_check = list(set([t.file for t in c.targets])) if c.targets else [c.target.file]
        for f in files_to_check:
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
                    expected += result.expected_targets
                    transformed += result.transformed_targets
                    missing += result.missing_targets
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
            
    # Recalculate targets based on deduplicated coverage
    expected_targets_count = len(dedup_coverage) if dedup_coverage else expected
    transformed_targets_count = sum(1 for tc in dedup_coverage if tc.status == "TRANSFORMED")
    missing_targets_count = expected_targets_count - transformed_targets_count
    
    rewrite_coverage = round(transformed_targets_count / expected_targets_count, 3) if expected_targets_count > 0 else 1.0
    overall_status = "PASS" if not all_violations and (expected_targets_count == 0 or missing_targets_count == 0) else "FAIL"

    return VerificationResult(
        status=overall_status,
        violations=all_violations,
        checks=all_checks,
        summary=f"Verification {'passed' if overall_status == 'PASS' else 'failed'} with {len(all_violations)} violations.",
        target_coverage=dedup_coverage,
        expected_targets=expected_targets_count,
        transformed_targets=transformed_targets_count,
        missing_targets=missing_targets_count,
        rewrite_coverage=rewrite_coverage
    )

def serialize_verification_results(result: VerificationResult) -> dict:
    return result.model_dump()
