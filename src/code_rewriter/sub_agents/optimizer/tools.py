"""Tools for the optimizer agent — optimization context and surgical function replacement."""

import difflib
import os
import re
from pathlib import Path
from google.adk.tools import ToolContext

from src.code_rewriter._common import (
    _maybe_parse,
    extract_function_source_by_name,
    format_intent_lines,
)
from src.code_rewriter.models.feedback_models import (
    build_repair_issues,
    render_repair_request,
)
from src.code_rewriter.models.rewrite_models import RewriteContract
from src.code_rewriter.tools.ast_replacer import function_ast_dump, replace_function_ast
from src.code_rewriter.tools.contract_verifier import verify_contract
from src.code_rewriter.tools.pipeline_analysis import verify_all_contracts
from src.code_rewriter.tools.transformation_risk import (
    analyze_candidate,
    analyze_transformation,
)

_ADCO_TAG_RE = re.compile(r"^[#-]{1,2}\s*ADCO_OPTIMIZED:.*\n?", re.MULTILINE)


_EXT_TO_COMMENT = {
    ".py": "#",
    ".sql": "--",
}


def _add_tag(path: str, content: str, sandbox_id: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    prefix = _EXT_TO_COMMENT.get(ext)
    if not prefix:
        return content
    content = _ADCO_TAG_RE.sub("", content)
    tag = f"{prefix} ADCO_OPTIMIZED: {sandbox_id}\n"
    return tag + content


def _coverage_feedback(tool_context: ToolContext) -> str:
    """Soft coverage line for write-tool responses (P1-E)."""
    contracts_data = tool_context.state.get("rewrite_contracts")
    if not contracts_data:
        return ""
    try:
        contracts = [
            RewriteContract(**c) if isinstance(c, dict) else c
            for c in contracts_data
        ]
    except Exception:
        return ""
    result = verify_all_contracts(
        tool_context.state.get("target", ""),
        tool_context.state.get("sandbox", ""),
        contracts,
        tool_context.state.get("modified_files", []),
    )
    return (
        f"\nCoverage: transformed {result.transformed_targets}/{result.expected_targets}, "
        f"missing {result.missing_targets}, coverage={result.rewrite_coverage:.0%}"
    )


def _transformation_risk(tool_context: ToolContext):
    """Return the advisory transformation-risk report for the current target.

    Returns ``None`` when the read/write map or target/sandbox paths are absent,
    or when the comparison cannot be computed. Never raises.
    """
    read_write_map = tool_context.state.get("read_write_map")
    if not read_write_map:
        return None
    contract = tool_context.state.get("current_contract")
    target = tool_context.state.get("target", "")
    sandbox = tool_context.state.get("sandbox", "")
    if not contract or not target or not sandbox:
        return None
    try:
        return analyze_transformation(target, sandbox, contract, read_write_map)
    except Exception:
        return None


def _risk_advisory(tool_context: ToolContext) -> str:
    """Compact advisory line appended to write-tool responses (never rejects)."""
    report = _transformation_risk(tool_context)
    if report is None:
        return ""
    flags = ", ".join(report.flags) if report.flags else "none"
    return f"\nTransformation risk: {report.risk} ({flags})"


def _render_transformation_risk(tool_context: ToolContext) -> str:
    """Render the `## Transformation Risk` section, or "" when not computable."""
    report = _transformation_risk(tool_context)
    if report is None:
        return ""
    lines = [
        "## Transformation Risk",
        f"- Risk: {report.risk}",
        f"- Flags: {', '.join(report.flags) if report.flags else 'none'}",
    ]
    for item in report.evidence:
        lines.append(f"- {item}")
    lines.append("Prefer a dependency-preserving shape when risk is HIGH.")
    return "\n".join(lines)


def _new_error_violations(before, after) -> list:
    """Return ERROR violations present in *after* but not in *before*."""
    before_signatures = {
        (v.code, v.message)
        for v in before.violations
        if v.severity == "ERROR"
    }
    new_errors = []
    seen: set[tuple[str, str]] = set()
    for violation in after.violations:
        if violation.severity != "ERROR":
            continue
        signature = (violation.code, violation.message)
        if signature in before_signatures or signature in seen:
            continue
        seen.add(signature)
        new_errors.append(violation)
    return new_errors


def _write_time_gate(
    tool_context: ToolContext, path: str, current_source: str, candidate_source: str
) -> str:
    """Reject a candidate that introduces NEW verification errors (shift-left).

    Returns an error string when the candidate must be rejected, else "".
    """
    contract_data = tool_context.state.get("current_contract")
    if not isinstance(contract_data, dict) or not contract_data:
        return ""

    target_dir = tool_context.state.get("target", "")
    if not target_dir:
        return ""

    original_path = os.path.join(target_dir, path)
    if not os.path.isfile(original_path):
        return ""

    try:
        original_source = Path(original_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

    try:
        contract = RewriteContract(**contract_data)
    except Exception:
        return ""

    try:
        before = verify_contract(original_source, current_source, contract)
        after = verify_contract(original_source, candidate_source, contract)
    except Exception:
        return ""

    new_errors = _new_error_violations(before, after)
    if not new_errors:
        return ""

    lines = [
        f"ERROR: rejected — this change introduces {len(new_errors)} verification error(s):"
    ]
    lines.extend(f"- [{v.code}] {v.message}" for v in new_errors)
    if any(v.code == "STRATEGY_NOT_APPLIED" for v in new_errors):
        lines.append(
            "For residual loop calls, compute the full key set and issue ONE set-based "
            "statement; for multi-column keys use a composite-key batch such as "
            "`(a, b) IN ((%s, %s), ...)` (or `a = ANY(%s) AND b IN (...)`). Do not loop "
            "in Python and query once per group."
        )
    lines.append("Fix these and call replace_function again.")
    return "\n".join(lines)


def _risk_block(
    tool_context: ToolContext, path: str, candidate_source: str
) -> str:
    """Block a candidate that is a HIGH-risk transformation.

    Returns an error string when the candidate must be rejected, else "".  Any
    failure while analysing risk is treated as "no block" so only a genuine HIGH
    classification can stop a write.
    """
    read_write_map = tool_context.state.get("read_write_map")
    if not read_write_map:
        return ""

    contract_data = tool_context.state.get("current_contract")
    if not isinstance(contract_data, dict) or not contract_data:
        return ""

    target = contract_data.get("target")
    if not isinstance(target, dict):
        return ""
    function = target.get("qualified_function") or target.get("function") or ""
    if not function:
        return ""

    target_dir = tool_context.state.get("target", "")
    if not target_dir:
        return ""
    original_path = os.path.join(target_dir, path)
    if not os.path.isfile(original_path):
        return ""

    try:
        original_source = Path(original_path).read_text(
            encoding="utf-8", errors="replace"
        )
    except Exception:
        return ""

    try:
        risk = analyze_candidate(
            original_source, candidate_source, function, read_write_map
        )
    except Exception:
        return ""

    if risk is None or risk.risk != "HIGH":
        return ""

    rejections = tool_context.state.setdefault("risk_rejections", [])
    if function not in rejections:
        rejections.append(function)

    flags = ", ".join(risk.flags) if risk.flags else "none"
    lines = [f"ERROR: rejected — HIGH transformation risk (blocked): {flags}."]
    lines.append("Evidence:")
    lines.extend(f"- {item}" for item in risk.evidence)
    lines.append(
        "Required: apply a dependency-preserving, lower-risk shape — e.g. keep the "
        "dependent lookup as a scalar subquery (do NOT expand the aggregate's "
        "top-level join), or keep the statements separate. Do not merge a "
        "value-dependent lookup into a larger join."
    )
    return "\n".join(lines)


def replace_function(
    path: str,
    function_name: str,
    new_function_code: str,
    tool_context: ToolContext,
) -> str:
    """Surgically replace a single function or class method in a Python file.

    Args:
        path: Relative path to the file within the sandbox.
        function_name: Name of the function or method to replace (e.g. 'process_batch' or 'DatabaseHandler.process_batch').
        new_function_code: Complete source code of the replacement function.
    """
    if not path:
        return "ERROR: path is required"
    sandbox = tool_context.state.get("sandbox", "")
    full = os.path.join(sandbox, path) if sandbox else path

    if not os.path.isfile(full):
        return f"ERROR: File not found: {path}"

    try:
        content = Path(full).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"ERROR: Could not read file '{path}': {e}"

    success, reconstructed, err = replace_function_ast(content, function_name, new_function_code)
    if not success:
        return f"ERROR: Failed to replace function '{function_name}': {err}"

    reconstructed_untagged = _ADCO_TAG_RE.sub("", reconstructed)
    if reconstructed_untagged == _ADCO_TAG_RE.sub("", content):
        return (
            f"ERROR: replacement for '{function_name}' in {path} is identical to the original. "
            f"You must ACTUALLY OPTIMIZE the database interaction code — apply batching, "
            f"hoist queries out of loops, and re-call replace_function."
        )

    # Text can differ while the function's AST is unchanged (whitespace/comments/
    # formatting only). The deterministic verifier compares ASTs, so reject such
    # no-op rewrites here to keep the write gate and the verifier consistent.
    original_dump = function_ast_dump(content, function_name)
    candidate_dump = function_ast_dump(reconstructed, function_name)
    if original_dump is not None and original_dump == candidate_dump:
        return (
            f"ERROR: replacement for '{function_name}' in {path} is structurally "
            f"(AST) identical to the original — only formatting/comments changed. "
            f"You must ACTUALLY change the database interaction code (queries, "
            f"batching, loop structure) and re-call replace_function."
        )

    rejection = _write_time_gate(tool_context, path, content, reconstructed)
    if rejection:
        return rejection

    risk_block = _risk_block(tool_context, path, reconstructed)
    if risk_block:
        return risk_block

    sandbox_id = os.path.basename(sandbox) if sandbox else ""
    reconstructed = _add_tag(path, reconstructed, sandbox_id)

    try:
        Path(full).write_text(reconstructed, encoding="utf-8")
    except Exception as e:
        return f"ERROR: Could not write file '{path}': {e}"

    modified_files = tool_context.state.setdefault("modified_files", [])
    if path not in modified_files:
        modified_files.append(path)

    return (
        f"Successfully replaced function '{function_name}' in '{path}'"
        + _coverage_feedback(tool_context)
        + _risk_advisory(tool_context)
    )


def _render_query_catalog(context_entry: dict) -> str:
    """Render the ground-truth query catalog (anti-hallucination)."""
    catalog = context_entry.get("query_catalog") or []
    query_dict = context_entry.get("query_dict")
    if not catalog and not query_dict:
        return ""

    lines = ["## Query Catalog"]
    lines.append(
        "Use ONLY the exact query keys and SQL below. Never invent a query key "
        "or a column name."
    )
    lines.append(
        "SQL shown for a dynamic template is UNFORMATTED; reuse it with the "
        "runtime variable and never hardcode displayed dummy values."
    )
    for entry in catalog:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key") or ""
        call = entry.get("call") or ""
        sql = entry.get("sql") or ""
        operation = entry.get("operation") or "OTHER"
        label = f"`{key}`" if key else "(inline SQL)"
        via = f" via `{call}`" if call else ""
        lines.append(f"- {label}{via} [{operation}]:")
        lines.append("  ```sql")
        for sql_line in (sql or "(unresolved)").splitlines():
            lines.append(f"  {sql_line}")
        lines.append("  ```")
    if isinstance(query_dict, dict) and query_dict:
        keys = ", ".join(sorted(query_dict.keys()))
        lines.append(f"- Available query keys in this dict: {keys}")
    return "\n".join(lines)


def _render_previous_attempt(
    state: dict, file: str, qualified: str, original_source: str
) -> list[str]:
    """Render the previous sandbox attempt and its diff against the original."""
    sandbox = state.get("sandbox", "")
    previous = "(not modified yet)"
    if sandbox and file:
        full = os.path.join(sandbox, file)
        if os.path.isfile(full):
            try:
                sandbox_source = Path(full).read_text(encoding="utf-8", errors="replace")
            except Exception:
                sandbox_source = ""
            extracted = extract_function_source_by_name(sandbox_source, qualified)
            if extracted:
                previous = extracted

    sections = ["## Your Previous Attempt\n```python\n" + previous + "\n```"]

    diff = difflib.unified_diff(
        (original_source or "").splitlines(),
        previous.splitlines(),
        fromfile="original",
        tofile="sandbox",
        lineterm="",
    )
    diff_text = "\n".join(diff) or "(no changes)"
    sections.append("## Diff vs Original\n```diff\n" + diff_text + "\n```")
    return sections


def _resolve_target_context(
    target_context_map: object, qualified: str, function: str
) -> dict:
    """Resolve the per-target context entry from ``target_context_map``."""
    if not isinstance(target_context_map, dict):
        return {}
    for key in (qualified, function):
        if key and isinstance(target_context_map.get(key), dict):
            return target_context_map[key]
    name = function or qualified
    if name:
        for key, value in target_context_map.items():
            if isinstance(key, str) and key.endswith(name) and isinstance(value, dict):
                return value
    return {}


def _render_contract(contract: dict, file: str, qualified: str) -> str:
    """Render the Contract section for the single target function."""
    lines = ["## Contract"]
    lines.append(f"- Rewrite ID: {contract.get('rewrite_id', '')}")
    lines.append(f"- Pattern: {contract.get('pattern', '')}")
    lines.append(f"- Strategy: {contract.get('strategy', '')}")
    allowed = contract.get("allowed_regions") or []
    lines.append(f"- Allowed edit regions: {', '.join(allowed) if allowed else '(none)'}")
    must_preserve = contract.get("must_preserve") or []
    lines.append(f"- Must preserve: {', '.join(must_preserve) if must_preserve else '(none)'}")
    must_not_change = contract.get("must_not_change") or []
    lines.append(f"- Must not change: {', '.join(must_not_change) if must_not_change else '(none)'}")
    lines.append(f"- Target file: {file}")
    lines.append(f"- Target function: {qualified}")
    return "\n".join(lines)


def _render_checklist(pattern: str) -> str:
    """Render the deterministic acceptance checklist for the target."""
    checklist = ["## Acceptance Checklist (deterministic — all must hold)"]
    pattern_upper = (pattern or "").upper()
    if "N+1" in pattern_upper or "N_PLUS_ONE" in pattern_upper:
        checklist.append(
            "- 0 database operations inside any loop (strict zero — no "
            "cursor.execute/executemany in loop body)"
        )
        checklist.append(
            "- at least one replacement DB operation outside the loop (batch IN/ANY/JOIN "
            "before the loop, or executemany after it)"
        )
    checklist.extend(
        [
            "- function signature unchanged",
            "- return statements and their values preserved",
            "- transaction and error-handling behavior preserved",
            "- do NOT invent column/table identifiers — reuse identifiers verbatim from the original SQL",
            "- optimize ONLY this function via replace_function; do not touch other functions",
        ]
    )
    return "\n".join(checklist)


def get_optimization_context(tool_context: ToolContext) -> str:
    """Return the single-target optimization context from session state.

    Everything the optimizer needs is provided here: the target contract, the
    function analysis, the exact target function source, the dependency slice,
    the acceptance checklist, and any prior failure to fix. The optimizer must
    not read the file itself.
    """
    intent_output = _maybe_parse(tool_context.state.get("intent_extractor_output"))
    if not intent_output:
        return "ERROR: intent_extractor_output not set in state — call intent_extractor first"

    current_contract = tool_context.state.get("current_contract")
    if not isinstance(current_contract, dict) or not current_contract:
        return "ERROR: no current_contract in state — the workflow must set it"

    target = current_contract.get("target") or {}
    file = target.get("file", "")
    qualified = target.get("qualified_function") or ""
    function = target.get("function") or ""
    display_function = qualified or function
    pattern = current_contract.get("pattern", "")

    context_entry = _resolve_target_context(
        tool_context.state.get("target_context_map"), qualified, function
    )

    sections = [_render_contract(current_contract, file, display_function)]

    analysis_summary = context_entry.get("analysis_summary") or ""
    if analysis_summary:
        sections.append("## Function Analysis\n" + analysis_summary)

    function_source = context_entry.get("function_source") or ""
    if function_source:
        sections.append(
            "## Target Function Source\n```python\n" + function_source + "\n```"
        )

    sections.extend(
        _render_previous_attempt(
            tool_context.state, file, display_function, function_source
        )
    )

    query_catalog = _render_query_catalog(context_entry)
    if query_catalog:
        sections.append(query_catalog)

    sections.append(_render_checklist(pattern))

    transformation_risk = _render_transformation_risk(tool_context)
    if transformation_risk:
        sections.append(transformation_risk)

    dependency_slice = context_entry.get("dependency_slice") or ""
    if dependency_slice:
        sections.append("## Target Dependency Slice\n" + dependency_slice)

    intent_text = format_intent_lines(intent_output, always_notes=True)
    sections.append("## Intent\n" + intent_text)

    strategies = tool_context.state.get("strategies", "")
    if strategies:
        sections.append(f"## Optimization strategies\n{strategies}")

    last_failure = tool_context.state.get("last_failure")
    if isinstance(last_failure, dict) and last_failure.get("status") == "FAIL":
        sections.append(render_repair_request(build_repair_issues(last_failure)))

    sandbox = tool_context.state.get("sandbox", "")
    if sandbox:
        sections.append(f"## Sandbox directory\n{sandbox}")

    attempt_count = tool_context.state.get("attempt_count")
    if attempt_count is not None:
        sections.append(f"## Attempt\nattempt_count={attempt_count}")

    return "\n\n".join(s for s in sections if s)
