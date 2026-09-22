import os

from .ast_analyzer import analyze_file
from ..models.ast_models import FileAnalysis
from ..models.rewrite_models import RewriteContract, RewriteTarget
from .rewrite_contract import build_rewrite_contract
from .rewrite_verifier import verify_rewrite, VerificationResult, TargetStatus, VerificationCheck, VerificationViolation
from .dependency_graph import build_dependency_graph, slice_dependency_graph, format_dependency_slice_markdown

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

_LOOP_STRATEGY_HINTS = ("N_PLUS_ONE", "BATCH", "COMBINING", "LOOP", "ROUND_TRIP")


def _pick_strategy(
    selected_strategy_names: list[str] | None,
    has_loop: bool,
    default: str,
) -> str:
    if not selected_strategy_names:
        return default
    if has_loop:
        for name in selected_strategy_names:
            if any(hint in name.upper() for hint in _LOOP_STRATEGY_HINTS):
                return name
    return selected_strategy_names[0]


def build_contracts_from_intent(
    target_dir: str, 
    intent_output: dict, 
    strategy: str = "COMBINING_QUERIES", 
    pattern: str = "N_PLUS_ONE_QUERY",
    selected_strategy_names: list[str] | None = None,
) -> tuple[dict[str, FileAnalysis], list[RewriteContract]]:
    targets = intent_output.get("optimization_targets", [])
    analyses: dict[str, FileAnalysis] = {}
    
    # (rel_path, FunctionAnalysis, has_loop) in deterministic discovery order.
    discovered: list[tuple[str, object, bool]] = []
    
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
                
            already_added: set[tuple[str, str]] = set()

            for fn in funcs_to_check:
                if is_ddl_or_setup_func(fn):
                    continue

                db_ops = fn.database_operations
                has_loop_db = any(op.inside_loop for op in db_ops)

                sequential_execute_ops = [
                    op for op in db_ops
                    if op.operation_type in ("EXECUTE", "EXECUTEMANY") and not op.inside_loop
                ]
                has_sequential_chain = (
                    not has_loop_db
                    and fn.control_flow.for_loops == 0
                    and fn.control_flow.while_loops == 0
                    and len(sequential_execute_ops) >= 2
                )

                if has_loop_db or has_sequential_chain:
                    key = (rel_path, fn.name)
                    if key not in already_added:
                        already_added.add(key)
                        discovered.append((rel_path, fn, has_loop_db))
        except Exception:
            continue
            
    contracts = []
    for rel_path, fn, has_loop in discovered:
        file_analysis = analyses.get(rel_path)
        if file_analysis is None:
            continue

        target = RewriteTarget(
            file=rel_path,
            function=fn.name,
            qualified_function=fn.qualified_name,
            source_location=fn.source_location,
        )
        contract_pattern = pattern if has_loop else "SEQUENTIAL_CHAIN"
        contract_strategy = _pick_strategy(selected_strategy_names, has_loop, strategy)
        contract = build_rewrite_contract(
            analysis=file_analysis,
            target=target,
            pattern=contract_pattern,
            strategy=contract_strategy,
        )
        contract.targets = [RewriteTarget(
            file=rel_path,
            function=fn.name,
            qualified_function=fn.qualified_name,
            source_location=fn.source_location,
        )]
        contract.allowed_regions = [fn.qualified_name or fn.name]
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
        if c.targets:
            all_contract_targets.extend(c.targets)
        elif c.target:
            all_contract_targets.append(c.target)
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
            
    # Expected = ALL contract targets (not only those covered by active contracts).
    # Uncovered contract targets count as MISSING_REWRITE so partial/no-op runs cannot PASS vacuously.
    covered_by_key: dict[tuple[str, str], TargetStatus] = {
        (tc.file, tc.function): tc for tc in dedup_coverage
    }
    final_coverage: list[TargetStatus] = []
    for t in all_contract_targets:
        fn = t.qualified_function or t.function
        match = covered_by_key.get((t.file, fn)) or covered_by_key.get((t.file, t.function or ""))
        if not match:
            for (f, fn_key), tc in covered_by_key.items():
                if f != t.file:
                    continue
                if t.function and (fn_key == t.function or fn_key.endswith("." + t.function)):
                    match = tc
                    break
        if match:
            final_coverage.append(match)
        else:
            final_coverage.append(TargetStatus(
                file=t.file,
                function=fn,
                status="MISSING_REWRITE",
                details="Target not verified (file not modified or target not covered)",
            ))

    expected_targets = expected_targets_count
    transformed_targets = sum(1 for tc in final_coverage if tc.status == "TRANSFORMED")
    missing_targets = sum(1 for tc in final_coverage if tc.status == "MISSING_REWRITE")
    rewrite_coverage = round(transformed_targets / expected_targets, 3) if expected_targets > 0 else 1.0

    critical_codes = {
        "SYNTAX_ERROR", "ORIGINAL_SYNTAX_ERROR", "TARGET_MISSING",
        "FUNCTION_SIGNATURE_CHANGED", "UNAUTHORIZED_CHANGE",
        "DB_OPERATION_REMOVED", "INVALID_REWRITE", "BREAKING_DEPENDENCY",
        "MISSING_REWRITE", "STRATEGY_NOT_APPLIED", "UNTRANSFORMED_FUNCTION", "UNTRANSFORMED_TARGET"
    }
    has_critical = any(v.severity == "ERROR" and v.code in critical_codes for v in all_violations)

    if has_critical or (expected_targets > 0 and (transformed_targets < expected_targets or missing_targets > 0)):
        overall_status = "FAIL"
    else:
        overall_status = "PASS" if not all_violations else "FAIL"

    return VerificationResult(
        status=overall_status,
        violations=all_violations,
        checks=all_checks,
        summary=f"Verification {'passed' if overall_status == 'PASS' else 'failed'} with {len(all_violations)} violations.",
        target_coverage=final_coverage,
        expected_targets=expected_targets,
        transformed_targets=transformed_targets,
        missing_targets=missing_targets,
        rewrite_coverage=rewrite_coverage
    )

def verify_target(
    target_dir: str,
    sandbox_dir: str,
    contract: RewriteContract,
) -> VerificationResult:
    rel = contract.target.file
    orig_path = os.path.join(target_dir, rel)
    opt_path = os.path.join(sandbox_dir, rel)

    orig_code = None
    opt_code = None
    try:
        with open(orig_path, "r", encoding="utf-8", errors="replace") as f:
            orig_code = f.read()
        with open(opt_path, "r", encoding="utf-8", errors="replace") as f:
            opt_code = f.read()
    except Exception:
        pass

    if orig_code is None or opt_code is None:
        fn_name = contract.target.qualified_function or contract.target.function or ""
        details = f"Target file '{rel}' missing from target or sandbox directory."
        return VerificationResult(
            status="FAIL",
            violations=[VerificationViolation(
                code="MISSING_REWRITE",
                severity="ERROR",
                message=details,
            )],
            checks=[VerificationCheck(
                name="check_target_file",
                status="FAIL",
                details=details,
            )],
            summary="Verification failed: target file missing.",
            target_coverage=[TargetStatus(
                file=rel,
                function=fn_name,
                status="MISSING_REWRITE",
                details=details,
            )],
            expected_targets=1,
            transformed_targets=0,
            missing_targets=1,
            rewrite_coverage=0.0,
        )

    return verify_rewrite(orig_code, opt_code, contract)


def _build_dependency_slice_pairs(
    analysis: dict[str, FileAnalysis] | FileAnalysis,
    contracts: list[RewriteContract],
    target_dir: str | None = None,
) -> list[tuple[str, str]]:
    analyses_map: dict[str, FileAnalysis] = {}
    if isinstance(analysis, dict):
        analyses_map = analysis
    elif isinstance(analysis, FileAnalysis):
        analyses_map = {analysis.file_path or "": analysis}

    pairs: list[tuple[str, str]] = []
    for contract in contracts:
        targets_to_check = contract.targets if contract.targets else ([contract.target] if contract.target else [])
        for t in targets_to_check:
            file_analysis = analyses_map.get(t.file)
            if not file_analysis:
                for k, v in analyses_map.items():
                    if k.endswith(t.file) or t.file.endswith(k):
                        file_analysis = v
                        break
            if not file_analysis:
                continue

            source_code = ""
            if target_dir and t.file:
                abs_path = os.path.join(target_dir, t.file)
                if os.path.exists(abs_path):
                    try:
                        with open(abs_path, "r", encoding="utf-8") as f:
                            source_code = f.read()
                    except Exception:
                        pass
            if not source_code and file_analysis.file_path and os.path.exists(file_analysis.file_path):
                try:
                    with open(file_analysis.file_path, "r", encoding="utf-8") as f:
                        source_code = f.read()
                except Exception:
                    pass

            fn_name = t.qualified_function or t.function
            if not fn_name:
                continue

            try:
                graph = build_dependency_graph(file_analysis, source_code)
                slice_data = slice_dependency_graph(graph, fn_name)
                md = format_dependency_slice_markdown(slice_data, contract)
                pairs.append((fn_name, md))
            except Exception:
                continue

    return pairs


def format_dependency_slice_map(
    analyses: dict[str, FileAnalysis] | FileAnalysis,
    contracts: list[RewriteContract],
    target_dir: str | None = None,
) -> dict[str, str]:
    """Return a mapping of target function name -> dependency slice markdown."""
    return dict(_build_dependency_slice_pairs(analyses, contracts, target_dir))


def format_pipeline_analysis_markdown(
    analysis: dict[str, FileAnalysis] | FileAnalysis,
    contracts: list[RewriteContract],
    target_dir: str | None = None,
) -> str:
    """Format pipeline analysis and dependency slices for all contract targets into markdown.

    For each target in contracts:
      - Builds DependencyGraph using build_dependency_graph(file_analysis, source_code).
      - Slices the graph using slice_dependency_graph(graph, target_fn).
      - Formats the slice using format_dependency_slice_markdown(slice_data, contract).
      - Appends the dependency slice to the markdown analysis output.
    """
    pairs = _build_dependency_slice_pairs(analysis, contracts, target_dir)
    return "\n\n".join(md for _key, md in pairs)
