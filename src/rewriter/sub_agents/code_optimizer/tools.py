"""Tools for the code optimizer agent — sandbox file read/write/list."""

import json
import os
import re
from pathlib import Path
from google.adk.tools import ToolContext


def _maybe_parse(value: object) -> dict:
    """Return *value* as a dict, JSON-parsing strings (stripping markdown fences)."""
    if isinstance(value, str):
        stripped = re.sub(r"^```[a-z]*\n?", "", value.strip(), flags=re.MULTILINE)
        stripped = re.sub(r"```$", "", stripped.strip())
        try:
            return json.loads(stripped.strip())
        except (json.JSONDecodeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}

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
    return f"OK: wrote {len(content)} bytes to {path}"


def get_optimization_context(tool_context: ToolContext) -> str:
    """Return the intent, optimization strategies, files to optimize, and any prior
    verifier feedback from session state.

    On retry attempts (after a verifier FAIL), this also includes the verifier's
    failure category, reason, and detail so the optimizer can fix specific issues.
    """
    intent_output = _maybe_parse(tool_context.state.get("intent_extractor_output"))
    if not intent_output:
        return "ERROR: intent_extractor_output not set in state — call intent_extractor first"
    optimization_targets = intent_output.get("optimization_targets") or []
    if not optimization_targets:
        return "ERROR: no optimization_targets in intent_extractor_output — call intent_extractor first"
    strategies = tool_context.state.get("strategies", "")
    sandbox = tool_context.state.get("sandbox", "")

    intent_lines = [
        f"CONNECTION: {intent_output.get('connection', '')}",
        f"QUERIES: {intent_output.get('queries', '')}",
        f"TRANSACTIONS: {intent_output.get('transactions', '')}",
        f"N_PLUS_ONE: {intent_output.get('n_plus_one', '')}",
        f"CONCURRENCY: {intent_output.get('concurrency', '')}",
        f"ORM: {intent_output.get('orm', '')}",
        f"NOTES: {intent_output.get('notes', '')}",
        "OPTIMIZATION TARGETS:",
    ]
    for target in optimization_targets:
        intent_lines.append(f"- {target.get('file', '')}: {target.get('description', '')}")
    sections = ["## Intent\n" + "\n".join(intent_lines)]
    sections.append(f"## Optimization strategies\n{strategies}")
    files_to_optimize = [t.get("file", "") for t in optimization_targets]
    sections.append("## Files to optimize\n" + "\n".join(f"- {f}" for f in files_to_optimize))
    if sandbox:
        sections.append(f"## Sandbox directory\n{sandbox}")

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

    return "\n\n".join(sections)


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