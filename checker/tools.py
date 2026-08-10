"""Deterministic tools for the checker agent — find modified files, read files, list sandbox."""

import os
import re
from pathlib import Path
from google.adk.tools import ToolContext

# Regex pattern to detect ADCO_OPTIMIZED tags
_ADCO_TAG = re.compile(r"^[#-]{1,2}\s*ADCO_OPTIMIZED:")


def find_modified_files(tool_context: ToolContext) -> str:
    """Find all files in the sandbox that were modified by the ADCo rewriter.
    
    Scans the sandbox directory for files containing the provenance tag
    '# ADCO_OPTIMIZED:' (Python) or '-- ADCO_OPTIMIZED:' (SQL). Stores
    the list of relative paths in state['modified_files'].
    
    Returns a formatted string listing all found files.
    """
    sandbox = tool_context.state.get("sandbox", "")
    if not sandbox:
        return "ERROR: sandbox path not set in state"
    
    sandbox_path = Path(sandbox)
    if not sandbox_path.is_dir():
        return f"ERROR: sandbox directory not found: {sandbox}"
    
    modified = []
    for entry in sandbox_path.rglob("*"):
        if entry.is_file():
            try:
                content = entry.read_text(errors="ignore")
                if _ADCO_TAG.search(content):
                    rel = entry.relative_to(sandbox_path)
                    modified.append(str(rel))
            except (OSError, UnicodeDecodeError):
                continue
    
    # Sort for consistent output
    modified.sort()
    
    # Store in state
    tool_context.state["modified_files"] = modified
    
    if not modified:
        return "No modified files found — no files contain the ADCO_OPTIMIZED provenance tag."
    
    lines = [f"Found {len(modified)} modified file(s):"]
    for f in modified:
        # Get file size for context
        full = sandbox_path / f
        size = full.stat().st_size
        ext = full.suffix.lstrip(".") or "unknown"
        lines.append(f"  [{ext}] {f} ({size:,} bytes)")
    return "\n".join(lines)


def read_file(file: str, tool_context: ToolContext) -> str:
    """Read the contents of a file in the sandbox.
    
    Args:
        file: Relative path to the file within the sandbox directory.
    
    Rejects path traversal attempts (..) and paths that resolve outside
    the sandbox. Content is capped at a reasonable limit.
    
    Returns the file content or an error message.
    """
    MAX_BYTES = 50_000  # Cap file reads for token control
    
    sandbox = tool_context.state.get("sandbox", "")
    if not sandbox:
        return "ERROR: sandbox path not set in state"
    
    sandbox_path = Path(sandbox).resolve()
    
    # Path containment check
    file_path = sandbox_path / file
    try:
        resolved = file_path.resolve()
    except (OSError, ValueError):
        return f"ERROR: invalid file path: {file}"
    
    # Must be within sandbox
    try:
        resolved.relative_to(sandbox_path)
    except ValueError:
        return f"ERROR: path traversal rejected: {file} resolves outside sandbox"
    
    if not resolved.is_file():
        return f"ERROR: file not found: {file}"
    
    try:
        content = resolved.read_text()
    except UnicodeDecodeError:
        return f"ERROR: cannot read {file} — it appears to be a binary file"
    except OSError as e:
        return f"ERROR: cannot read {file}: {e}"
    
    if len(content) > MAX_BYTES:
        content = content[:MAX_BYTES]
        content += f"\n\n[... truncated at {MAX_BYTES:,} bytes; file is larger ...]"
    
    lines = content.split("\n")
    numbered = [f"{i+1:6d}| {line}" for i, line in enumerate(lines)]
    header = f"=== {file} ({len(content):,} bytes, {len(lines)} lines) ===\n"
    return header + "\n".join(numbered)


def read_original_file(file: str, tool_context: ToolContext) -> str:
    """Read the contents of a file from the original (pre-rewrite) codebase.

    Use this to compare the original version of a file against the
    optimized version returned by `read_file`. Every finding about
    regression, correctness, or safety must be verified by comparing
    the two versions — if the optimized code matches the original in
    the relevant area, there is NO issue.

    Args:
        file: Relative path to the file within the original directory.

    Returns the file content or an error message.
    """
    MAX_BYTES = 50_000

    original = tool_context.state.get("original", "")
    if not original:
        return (
            "ERROR: original path not set in state. "
            "Proceed with the optimized code alone — do NOT flag a regression "
            "issue unless you can prove the change is harmful solely from the "
            "optimized file."
        )

    original_path = Path(original).resolve()

    file_path = original_path / file
    try:
        resolved = file_path.resolve()
    except (OSError, ValueError):
        return f"ERROR: invalid file path: {file}"

    try:
        resolved.relative_to(original_path)
    except ValueError:
        return f"ERROR: path traversal rejected: {file} resolves outside original"

    if not resolved.is_file():
        return f"INFO: file not found in original: {file} (this file was likely created by the rewriter)"

    try:
        content = resolved.read_text()
    except UnicodeDecodeError:
        return f"ERROR: cannot read {file} — it appears to be a binary file"
    except OSError as e:
        return f"ERROR: cannot read {file}: {e}"

    if len(content) > MAX_BYTES:
        content = content[:MAX_BYTES]
        content += f"\n\n[... truncated at {MAX_BYTES:,} bytes; file is larger ...]"

    lines = content.split("\n")
    numbered = [f"{i+1:6d}| {line}" for i, line in enumerate(lines)]
    header = f"=== [ORIGINAL] {file} ({len(content):,} bytes, {len(lines)} lines) ===\n"
    return header + "\n".join(numbered)


def list_sandbox(tool_context: ToolContext) -> str:
    """List the directory tree of the sandbox for context exploration.
    
    Returns a tree-like directory listing of all files in the sandbox,
    with modified files marked with an asterisk (*).
    """
    sandbox = tool_context.state.get("sandbox", "")
    if not sandbox:
        return "ERROR: sandbox path not set in state"
    
    sandbox_path = Path(sandbox)
    if not sandbox_path.is_dir():
        return f"ERROR: sandbox directory not found: {sandbox}"
    
    modified = set(tool_context.state.get("modified_files", []))
    sandbox_str = str(sandbox_path)
    
    # Build a tree-like listing
    lines = [f"Sandbox: {sandbox_str}\n"]
    lines.append(sandbox_str + "/")
    
    for root, dirs, files in os.walk(sandbox_str):
        # Skip common non-code dirs
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".venv", "venv", "node_modules", ".git")]
        dirs.sort()
        
        level = root.replace(sandbox_str, "").count(os.sep)
        indent = "  " * level
        
        if root != sandbox_str:
            base = os.path.basename(root)
            lines.append(f"{indent}├── {base}/")
        
        sub_indent = "  " * (level + 1)
        for f in sorted(files):
            # Determine relative path for modified check
            try:
                rel = str(Path(root) / f)
                rel = rel[len(sandbox_str):].lstrip(os.sep)
            except ValueError:
                rel = f
            marker = " *" if rel in modified else ""
            lines.append(f"{sub_indent}├── {f}{marker}")
    
    return "\n".join(lines)
