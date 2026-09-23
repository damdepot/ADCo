import ast
import os

from .ast_analyzer import analyze_file
from .sql_resolver import collect_module_dicts, resolve
from ..models.ast_models import FileAnalysis, FunctionAnalysis
from ..models.rewrite_models import RewriteContract, RewriteTarget
from .rewrite_contract import build_rewrite_contract
from .contract_verifier import verify_contract, VerificationResult, TargetStatus, VerificationCheck, VerificationViolation
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

def verify_all_contracts(
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
                
                result = verify_contract(orig_code, opt_code, c)
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
        "MISSING_REWRITE", "STRATEGY_NOT_APPLIED", "UNTRANSFORMED_FUNCTION", "UNTRANSFORMED_TARGET",
        "MULTI_STATEMENT_EXECUTE", "DUPLICATE_WHERE", "UNKNOWN_QUERY_KEY",
        "IMPLICIT_CROSS_JOIN"
    }
    has_critical = any(v.severity == "ERROR" and v.code in critical_codes for v in all_violations)

    if has_critical or (expected_targets > 0 and (transformed_targets < expected_targets or missing_targets > 0)):
        overall_status = "FAIL"
    else:
        overall_status = "PASS" if not any(v.severity == "ERROR" for v in all_violations) else "FAIL"

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

def verify_contract_target(
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

    return verify_contract(orig_code, opt_code, contract)


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


def format_function_analysis_summary(fn: FunctionAnalysis | None) -> str:
    """Render a compact markdown summary of a single function analysis."""
    if fn is None:
        return ""

    decorators = ", ".join(fn.decorators) if fn.decorators else "none"
    params = ", ".join(fn.parameters)

    lines = [
        f"## Function Analysis: {fn.qualified_name}",
        f"- Signature: def {fn.name}({params})",
        f"- Source location: lines {fn.source_location.start_line}-{fn.source_location.end_line}",
        f"- Decorators: {decorators}",
        f"- Control flow: for_loops={fn.control_flow.for_loops}, while_loops={fn.control_flow.while_loops}",
        f"- Return statements: {fn.return_count}",
        f"- Database operations ({len(fn.database_operations)}):",
    ]

    for i, op in enumerate(fn.database_operations):
        inside = "True" if op.inside_loop else "False"
        lines.append(
            f"  - [{i}] {op.operation_type} / {op.sql_operation} / "
            f"inside_loop={inside} / line {op.source_location.start_line}"
        )
        lines.append(f"    SQL: {op.sql or '(dynamic)'}")

    call_names = sorted({c.call_name for c in fn.calls})
    lines.append(f"- Calls: {', '.join(call_names) if call_names else 'none'}")

    return "\n".join(lines)


def _find_function_analysis(
    analysis: FileAnalysis | None, qualified: str
) -> FunctionAnalysis | None:
    if analysis is None or not qualified:
        return None

    candidates: list[FunctionAnalysis] = list(analysis.functions)
    for cls in analysis.classes:
        candidates.extend(cls.methods)

    for fn in candidates:
        if fn.qualified_name == qualified or fn.name == qualified:
            return fn
    for fn in candidates:
        if fn.qualified_name.endswith(qualified):
            return fn
    return None


def _extract_function_source(source: str, source_location) -> str:
    if not source or source_location is None:
        return ""
    lines = source.splitlines()
    start = source_location.start_line
    end = source_location.end_line
    if start < 1 or end < start or end > len(lines):
        return ""
    return "\n".join(lines[start - 1:end])


_SQL_OPERATIONS = ("SELECT", "INSERT", "UPDATE", "DELETE")
_EXECUTE_METHODS = ("execute", "executemany")


def _const_str_key(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _operation_from_sql(sql: str) -> str:
    if not isinstance(sql, str) or not sql.strip():
        return "OTHER"
    head = sql.strip().upper()
    for name in _SQL_OPERATIONS:
        if head.startswith(name):
            return name
    return "OTHER"


def _find_function_node(tree: ast.Module, qualified: str):
    """Return the AST node for a (qualified) function name, or ``None``."""
    if not qualified:
        return None
    bare = qualified.rsplit(".", 1)[-1]

    def _walk(node: ast.AST, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                found = _walk(child, f"{prefix}{child.name}.")
                if found is not None:
                    return found
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                full = f"{prefix}{child.name}"
                if full == qualified or child.name == bare:
                    return child
                found = _walk(child, f"{full}.")
                if found is not None:
                    return found
            else:
                found = _walk(child, prefix)
                if found is not None:
                    return found
        return None

    return _walk(tree, "")


def _discover_query_context(
    source_code: str, fn: FunctionAnalysis | None, qualified: str
) -> tuple[list[dict], dict | None]:
    """Best-effort discovery of the target's query catalog and query dict.

    Returns ``(query_catalog, query_dict)`` where ``query_dict`` is ``None``
    when no ``name = MODULE_DICT["sub"]`` alias can be found in the function.
    """
    if not source_code or fn is None:
        return [], None
    try:
        tree = ast.parse(source_code)
    except (SyntaxError, ValueError):
        return [], None

    module_dicts = collect_module_dicts(tree)
    func_node = _find_function_node(tree, qualified)
    if func_node is None:
        return [], None

    local_vars: dict[str, dict] = {}
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Subscript):
            continue
        subscript = node.value
        if not isinstance(subscript.value, ast.Name):
            continue
        base = module_dicts.get(subscript.value.id)
        if not isinstance(base, dict):
            continue
        key = _const_str_key(subscript.slice)
        if key is None:
            continue
        sub_dict = base.get(key)
        if isinstance(sub_dict, dict):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local_vars[target.id] = sub_dict

    refs_by_sql: dict[str, list[dict]] = {}
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _EXECUTE_METHODS or not node.args:
            continue
        arg0 = node.args[0]
        if not isinstance(arg0, ast.Subscript):
            continue
        key = _const_str_key(arg0.slice)
        if key is None:
            continue
        sql = resolve(arg0, local_vars, module_dicts)
        if not isinstance(sql, str):
            sql = ""
        call = ast.get_source_segment(source_code, arg0) or ""
        refs_by_sql.setdefault(sql, []).append({"key": key, "call": call, "sql": sql})

    catalog: list[dict] = []
    for op in fn.database_operations:
        if not op.sql:
            continue
        candidates = refs_by_sql.get(op.sql)
        ref = candidates.pop(0) if candidates else None
        operation = op.sql_operation if op.sql_operation in _SQL_OPERATIONS else "OTHER"
        catalog.append(
            {
                "key": ref["key"] if ref else "",
                "call": ref["call"] if ref else "",
                "sql": op.sql,
                "operation": operation,
            }
        )

    query_dict: dict | None = None
    for sub_dict in local_vars.values():
        if isinstance(sub_dict, dict):
            query_dict = {k: v for k, v in sub_dict.items() if isinstance(v, str)}
            break

    return catalog, query_dict


def build_target_context_map(
    analyses: dict[str, FileAnalysis] | FileAnalysis,
    contracts: list[RewriteContract],
    target_dir: str | None = None,
) -> dict[str, dict]:
    """Build a per-target context map (analysis + source + dependency slice)."""
    analyses_map: dict[str, FileAnalysis] = {}
    if isinstance(analyses, dict):
        analyses_map = analyses
    elif isinstance(analyses, FileAnalysis):
        analyses_map = {analyses.file_path or "": analyses}

    slices = dict(_build_dependency_slice_pairs(analyses, contracts, target_dir))

    out: dict[str, dict] = {}
    for contract in contracts:
        targets_to_check = contract.targets if contract.targets else ([contract.target] if contract.target else [])
        for t in targets_to_check:
            qfn = t.qualified_function or t.function
            if not qfn:
                continue

            file_analysis = analyses_map.get(t.file)
            if not file_analysis and t.file:
                for k, v in analyses_map.items():
                    if k.endswith(t.file) or t.file.endswith(k):
                        file_analysis = v
                        break

            fn = _find_function_analysis(file_analysis, qfn)

            source_code = ""
            if target_dir and t.file:
                abs_path = os.path.join(target_dir, t.file)
                if os.path.exists(abs_path):
                    try:
                        with open(abs_path, "r", encoding="utf-8") as f:
                            source_code = f.read()
                    except Exception:
                        pass
            if not source_code and file_analysis and file_analysis.file_path and os.path.exists(file_analysis.file_path):
                try:
                    with open(file_analysis.file_path, "r", encoding="utf-8") as f:
                        source_code = f.read()
                except Exception:
                    pass

            query_catalog, query_dict = _discover_query_context(source_code, fn, qfn)

            entry = {
                "analysis_summary": format_function_analysis_summary(fn),
                "function_source": _extract_function_source(
                    source_code, fn.source_location if fn else None
                ),
                "dependency_slice": slices.get(qfn, ""),
                "query_catalog": query_catalog,
            }
            if query_dict is not None:
                entry["query_dict"] = query_dict
            out[qfn] = entry

    return out
