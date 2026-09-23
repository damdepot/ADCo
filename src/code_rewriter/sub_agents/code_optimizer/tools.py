"""Tools for the code optimizer agent — sandbox file read/write/list."""

import os
import re
from pathlib import Path
from google.adk.tools import ToolContext

from src.code_rewriter._common import _maybe_parse, format_intent_lines
from src.code_rewriter.models.rewrite_models import RewriteContract
from src.code_rewriter.tools.ast_replacer import replace_function_ast
from src.code_rewriter.tools.pipeline_analysis import execute_deterministic_verification

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
    result = execute_deterministic_verification(
        tool_context.state.get("target", ""),
        tool_context.state.get("sandbox", ""),
        contracts,
        tool_context.state.get("modified_files", []),
    )
    return (
        f"\nCoverage: transformed {result.transformed_targets}/{result.expected_targets}, "
        f"missing {result.missing_targets}, coverage={result.rewrite_coverage:.0%}"
    )


def read_file(path: str, tool_context: ToolContext) -> str:
    """Read the contents of a file in the sandbox.

    Args:
        path: Relative path to the file within the sandbox.
    """
    sandbox = tool_context.state.get("sandbox", "")
    full = os.path.join(sandbox, path) if sandbox else path
    try:
        return Path(full).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"ERROR: {e}"


def write_file(path: str, content: str, tool_context: ToolContext) -> str:
    """Write a file to the sandbox. Validates Python syntax before committing.

    An ``ADCO_OPTIMIZED`` provenance tag is automatically prepended to
    ``.py`` and ``.sql`` files so the checker can find modified files.

    Args:
        path: Relative path to write to within the sandbox.
        content: Complete file contents to write. Use real newlines, not
            literal backslash-n. Do not escape quotes — write them as-is.
    """
    if not path:
        return "ERROR: path is required"
    sandbox = tool_context.state.get("sandbox", "")
    full = os.path.join(sandbox, path) if sandbox else path
    os.makedirs(os.path.dirname(full), exist_ok=True)

    sandbox_id = os.path.basename(sandbox) if sandbox else ""
    content = _add_tag(path, content, sandbox_id)

    if path.endswith(".py"):
        try:
            compile(content, path, "exec")
        except SyntaxError as e:
            return (
                f"ERROR: SyntaxError in {path} at line {e.lineno}: {e.msg}\n"
                f"The content you passed has a syntax error. Common causes:\n"
                f"- Literal '\\n' (backslash-n) instead of real newlines — pass actual newline characters\n"
                f"- Escaped quotes like \\' or \\\" outside strings — write quotes as-is\n"
                f"- Lines merged together (e.g. 'import loggingfrom pprint' instead of two lines)\n"
                f"Read the file again, fix the issue, and re-call write_file with corrected content."
            )

    if os.path.isfile(full):
        existing = Path(full).read_text(encoding="utf-8", errors="replace")
        existing_untagged = _ADCO_TAG_RE.sub("", existing)
        content_untagged = _ADCO_TAG_RE.sub("", content)
        if existing_untagged == content_untagged:
            return (
                f"ERROR: {path} is identical to the original. You must ACTUALLY "
                f"OPTIMIZE the database interaction code — not write it back unchanged. "
                f"Apply the optimization strategies: combine N+1 loops into single "
                f"queries, batch individual INSERT/UPDATE calls with executemany, "
                f"push filters into SQL WHERE clauses, eliminate redundant round-trips. "
                f"Read the file again, make real changes, and re-call write_file."
            )

    Path(full).write_text(content, encoding="utf-8")
    modified_files = tool_context.state.setdefault("modified_files", [])
    if path not in modified_files:
        modified_files.append(path)
    return f"OK: wrote {len(content)} bytes to {path}" + _coverage_feedback(tool_context)


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
    )


def _lookup_dependency_slice(
    pipeline_analysis_markdown: object, qualified: str, function: str
) -> str:
    """Resolve the dependency slice for one target function."""
    if not pipeline_analysis_markdown:
        return ""
    if isinstance(pipeline_analysis_markdown, str):
        needle = qualified or function
        return pipeline_analysis_markdown if needle and needle in pipeline_analysis_markdown else ""
    if isinstance(pipeline_analysis_markdown, dict):
        for key in (qualified, function):
            if key and key in pipeline_analysis_markdown:
                return pipeline_analysis_markdown[key] or ""
        name = function or qualified
        if name:
            for key, value in pipeline_analysis_markdown.items():
                if isinstance(key, str) and key.endswith(name):
                    return value or ""
    return ""


def _render_prior_failure(last_failure: dict) -> str:
    """Render the deterministic prior-failure section for a single target."""
    lines = ["## Prior failure (fix EXACTLY this)"]
    for entry in last_failure.get("target_coverage", []) or []:
        if entry.get("status") != "TRANSFORMED":
            lines.append(
                f"- {entry.get('file')}::{entry.get('function')} -> "
                f"{entry.get('status')}: {entry.get('details')}"
            )
    for violation in last_failure.get("violations", []) or []:
        lines.append(f"- [{violation.get('code')}] {violation.get('message')}")
    lines.append(
        "Fix ONLY the issues above. Preserve every other optimization already applied. "
        "Do not rewrite unrelated functions."
    )
    return "\n".join(lines)


def _render_single_target(
    tool_context: ToolContext, contract: dict, intent_output: dict
) -> str:
    """Render the single-target optimization context (one target function per run)."""
    target = contract.get("target") or {}
    file = target.get("file", "")
    qualified = target.get("qualified_function") or ""
    function = target.get("function") or ""
    display_function = qualified or function
    pattern = contract.get("pattern", "")
    strategy = contract.get("strategy", "")
    sections = []

    target_lines = ["## Current Target Function"]
    if file:
        target_lines.append(f"File: {file}")
    if display_function:
        target_lines.append(f"Function: {display_function}")
    if pattern:
        target_lines.append(f"Pattern: {pattern}")
    if strategy:
        target_lines.append(f"Strategy: {strategy}")
    source_location = target.get("source_location")
    if source_location:
        target_lines.append(
            f"Source location: lines {source_location.get('start_line')}–"
            f"{source_location.get('end_line')} (columns "
            f"{source_location.get('start_column')}–{source_location.get('end_column')})"
        )
    sections.append("\n".join(target_lines))

    checklist = ["## Acceptance Checklist (deterministic — all must hold)"]
    pattern_upper = pattern.upper()
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
    sections.append("\n".join(checklist))

    slice_text = _lookup_dependency_slice(
        tool_context.state.get("pipeline_analysis_markdown"), qualified, function
    )
    if slice_text:
        sections.append(f"## Target Dependency Slice\n{slice_text}")

    intent_text = format_intent_lines(intent_output, always_notes=True)
    sections.append("## Intent\n" + intent_text)

    strategies = tool_context.state.get("strategies", "")
    if strategies:
        sections.append(f"## Optimization strategies\n{strategies}")

    last_failure = tool_context.state.get("last_failure")
    if isinstance(last_failure, dict) and last_failure.get("status") == "FAIL":
        sections.append(_render_prior_failure(last_failure))

    sandbox = tool_context.state.get("sandbox", "")
    if sandbox:
        sections.append(f"## Sandbox directory\n{sandbox}")

    attempt_count = tool_context.state.get("attempt_count")
    if attempt_count is not None:
        sections.append(f"## Attempt\nattempt_count={attempt_count}")

    return "\n\n".join(s for s in sections if s)


def _render_legacy(tool_context: ToolContext, intent_output: dict) -> str:
    """Render the legacy (all-targets-at-once) optimization context."""
    optimization_targets = intent_output.get("optimization_targets") or []
    if not optimization_targets:
        return "ERROR: no optimization_targets in intent_extractor_output — call intent_extractor first"
    strategies = tool_context.state.get("strategies", "")
    sandbox = tool_context.state.get("sandbox", "")

    intent_text = format_intent_lines(
        intent_output, always_notes=True, targets_header="OPTIMIZATION TARGETS:"
    )
    sections = ["## Intent\n" + intent_text]
    sections.append(f"## Optimization strategies\n{strategies}")
    files_to_optimize = [t.get("file", "") for t in optimization_targets]
    sections.append("## Files to optimize\n" + "\n".join(f"- {f}" for f in files_to_optimize))
    if sandbox:
        sections.append(f"## Sandbox directory\n{sandbox}")

    attempt_count = tool_context.state.get("attempt_count")
    if attempt_count is not None:
        sections.append(f"## Attempt\nattempt_count={attempt_count} of 5 (orchestrator-enforced)")

    cov = _coverage_feedback(tool_context).strip()
    if cov:
        sections.append(f"## Current coverage\n{cov}")

    pipeline_analysis_markdown = tool_context.state.get("pipeline_analysis_markdown")
    if pipeline_analysis_markdown:
        if isinstance(pipeline_analysis_markdown, dict):
            rendered_slices = "\n\n".join(str(v) for v in pipeline_analysis_markdown.values())
        else:
            rendered_slices = pipeline_analysis_markdown
        if rendered_slices:
            sections.append(f"## Target Functions & Dependency Slices\n{rendered_slices}")

    verifier_output = _maybe_parse(tool_context.state.get("verifier_output"))
    if verifier_output and verifier_output.get("status") == "FAIL":

        failure_section = (
            "## Prior verifier failure (MUST FIX)\n"
            f"Category: {verifier_output.get('category', 'N/A')}\n"
            f"Reason: {verifier_output.get('reason', 'N/A')}\n"
            f"Detail: {verifier_output.get('detail', 'N/A')}\n"
        )
        suggestion = verifier_output.get("suggestion", "")
        if suggestion:
            failure_section += (
                f"SUGGESTION (follow this to fix the issue):\n"
                f"{suggestion}\n"
            )
        failure_section += (
            "You MUST fix the exact issue raised above. Do NOT make unrelated changes — "
            "focus only on addressing this specific failure while preserving the "
            "optimizations you have already applied."
        )
        sections.append(failure_section)

    contracts = tool_context.state.get("rewrite_contracts")
    if contracts:
        contract_lines = ["## Rewrite Contracts (Deterministic AST Analysis)"]
        for c in contracts:
            contract_lines.append(f"- Rewrite ID: {c.get('rewrite_id')}")
            contract_lines.append(f"  Strategy: {c.get('strategy')}")
            contract_lines.append(f"  Pattern: {c.get('pattern')}")
            contract_lines.append(f"  Must Preserve: {', '.join(c.get('must_preserve', []))}")
            contract_lines.append(f"  Must Not Change: {', '.join(c.get('must_not_change', []))}")
            targets = c.get('targets', [])
            if targets:
                contract_lines.append("  Targets:")
                for t in targets:
                    fn = t.get('qualified_function') or t.get('function')
                    contract_lines.append(f"    - File: {t.get('file')}, Function: {fn}")
        sections.append("\n".join(contract_lines))

    return "\n\n".join(sections)


def get_optimization_context(tool_context: ToolContext) -> str:
    """Return the optimization context from session state.

    When ``current_contract`` is set (one target function per workflow run), a
    single-target context is rendered: the target contract, its dependency slice,
    a deterministic acceptance checklist, and any prior failure to fix. Otherwise
    the legacy all-targets context is rendered.
    """
    intent_output = _maybe_parse(tool_context.state.get("intent_extractor_output"))
    if not intent_output:
        return "ERROR: intent_extractor_output not set in state — call intent_extractor first"

    current_contract = tool_context.state.get("current_contract")
    if isinstance(current_contract, dict) and current_contract:
        return _render_single_target(tool_context, current_contract, intent_output)

    return _render_legacy(tool_context, intent_output)


def list_sandbox(subdir: str = "", tool_context: ToolContext | None = None) -> str:
    """List files in the sandbox directory.

    Args:
        subdir: Subdirectory to list (empty string for root).
    """
    sandbox = tool_context.state.get("sandbox", "") if tool_context else ""
    base = os.path.join(sandbox, subdir) if subdir else sandbox
    if not os.path.isdir(base):
        return f"ERROR: directory not found: {base}"
    items = sorted(os.listdir(base))
    return "\n".join(items)