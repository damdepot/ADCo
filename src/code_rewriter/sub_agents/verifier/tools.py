"""Tools for the verifier agent — syntax check, compare original vs modified, run application."""

import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from google.adk.tools import ToolContext

from src.code_rewriter.models.rewrite_models import RewriteContract
from src.code_rewriter.tools.pipeline_analysis import execute_deterministic_verification

_DB_ERRORS = re.compile(
    r"OperationalError|Can't connect|Connection refused|"
    r"Unknown database|Access denied|could not translate host name|"
    r"could not connect to server|No such file or directory.*\.sock"
)
_ARGS_ERRORS = re.compile(
    r"usage:|error: the following arguments are required|"
    r"error: unrecognized arguments|error: argument"
)
_NETWORK_ERRORS = re.compile(
    r"ConnectionError|NetworkError|getaddrinfo|Name or service not known|"
    r"Connection timed out|Temporary failure in name resolution"
)


def _classify_failure(stderr: str, stdout: str) -> str | None:
    """Classify a startup failure from the captured output.

    Returns one of: ``MISSING_ARGS``, ``DB``, ``NETWORK``, ``CODE``, or ``None``.
    """
    combined = f"{stderr}\n{stdout}"
    if _ARGS_ERRORS.search(combined):
        return "MISSING_ARGS"
    if _DB_ERRORS.search(combined):
        return "DB"
    if _NETWORK_ERRORS.search(combined):
        return "NETWORK"
    # ValueError alone is too generic (may be env); require a traceback to call it CODE.
    if re.search(r"SyntaxError|ImportError|ModuleNotFoundError|NameError|AttributeError|TypeError|IndentationError", combined):
        return "CODE"
    if "ValueError" in combined and re.search(r"Traceback \(most recent call last\)", combined):
        return "CODE"
    return None


def _deserialize_contracts(contracts_data: list) -> list[RewriteContract]:
    """Coerce raw state entries into RewriteContract models (raises on invalid)."""
    return [RewriteContract(**c) if isinstance(c, dict) else c for c in contracts_data]


def _format_verification_details(result) -> list[str]:
    """Shared summary lines for deterministic verification output."""
    lines = [
        f"Deterministic Verification Status: {result.status}",
        f"Summary: {result.summary}",
        f"Expected targets: {result.expected_targets}, Transformed targets: {result.transformed_targets}, Missing targets: {result.missing_targets}",
        f"Rewrite coverage: {result.rewrite_coverage:.2%}",
    ]
    if result.violations:
        lines.append("Violations:")
        lines.extend(f"  - [{v.severity}] {v.code}: {v.message}" for v in result.violations)
    if result.target_coverage:
        lines.append("Target Coverage:")
        for tc in result.target_coverage:
            status_info = f"  - {tc.file}::{tc.function} -> {tc.status}"
            if tc.details:
                status_info += f" ({tc.details})"
            lines.append(status_info)
    return lines


def _verify_deterministic(tool_context: ToolContext | None) -> tuple[str, str | None]:
    """Run deterministic AST verification if contracts exist in tool_context.state.

    Returns (status, details) where status is 'PASS', 'FAIL', or 'NONE' (if no contracts).
    """
    if not tool_context or not hasattr(tool_context, "state") or not tool_context.state:
        return "NONE", None

    state = tool_context.state
    contracts_data = state.get("rewrite_contracts")
    if not contracts_data:
        return "NONE", None

    try:
        contracts = _deserialize_contracts(contracts_data)
    except Exception:
        return "NONE", None

    result = execute_deterministic_verification(
        state.get("target", ""),
        state.get("sandbox", ""),
        contracts,
        state.get("modified_files", []),
    )
    state["deterministic_verification"] = result.model_dump()

    if result.status == "FAIL":
        return "FAIL", "\n".join(_format_verification_details(result))

    return "PASS", None


def check_syntax(tool_context: ToolContext) -> str:
    """Syntax-check only the files modified by the code optimizer."""
    sandbox = tool_context.state.get("sandbox", "")
    modified = tool_context.state.get("modified_files", [])

    if not modified:
        return "No modified files to check"

    lines = []
    for rel in modified:
        if not rel.endswith(".py"):
            continue
        full = os.path.join(sandbox, rel)
        try:
            compile(Path(full).read_text(), rel, "exec")
            lines.append(f"  OK  {rel}")
        except SyntaxError as e:
            lines.append(f"  FAIL {rel}: {e}")
        except FileNotFoundError:
            lines.append(f"  MISSING {rel}")
    return "\n".join(lines) if lines else "No Python files to syntax-check"


def run_application(args: str = "", tool_context: ToolContext | None = None) -> str:
    """Launch the application and verify it starts without an immediate crash.

    Non-blocking startup check: the application is launched with Popen and only
    watched for a short window (3 seconds). It is NOT run to completion.

    Behavior:
    - If the process is still running after 3 seconds, it started cleanly
      without an immediate crash -> it is terminated and "STARTED_OK" is
      returned along with the command and any early stdout/stderr.
    - If the process exited within 3 seconds with returncode 0, "STARTED_OK"
      is returned along with the captured output.
    - If rewrite_contracts exist in state and fail deterministic AST
      verification, "STARTUP_FAILED_CODE:DETERMINISTIC_VERIFICATION_FAIL"
      is returned instead of "STARTED_OK".
    - If the process exited within 3 seconds with a non-zero returncode, the
      failure is classified into one of:

      * ``STARTUP_FAILED_ENV:MISSING_ARGS`` — the app needs CLI arguments
        (usage message, argument required error).  This is NOT a code error;
        the verifier should retry with ``--help`` or reasonable defaults.
      * ``STARTUP_FAILED_ENV:DB`` — a database server is not available
        (connection refused, unknown host, missing socket).  This is **not**
        a code error — the code is syntactically correct.
      * ``STARTUP_FAILED_ENV:NETWORK`` — a network resource is unreachable.
        This is also **not** a code error.
      * ``STARTUP_FAILED_CODE:DETERMINISTIC_VERIFICATION_FAIL`` — AST verification
        failed (residual loop queries, untransformed targets).
      * ``STARTUP_FAILED_CODE`` — a real code-level error was detected
        (SyntaxError, ImportError, NameError, AttributeError, TypeError).
        This means the optimized code is broken.
      * ``STARTUP_FAILED_CODE:UNKNOWN`` — the process exited non-zero but
        no known error pattern was matched.

    Args:
        args: Optional CLI arguments to pass. Start empty, then try --help,
              then try reasonable defaults if the app requires arguments.
    """
    sandbox = tool_context.state.get("sandbox", "") if tool_context else ""
    entry = ""
    if tool_context:
        fs_out = tool_context.state.get("file_selector_output", {})
        if hasattr(fs_out, "entry_point"):
            entry = fs_out.entry_point or ""
        elif isinstance(fs_out, str):
            try:
                parsed = json.loads(fs_out)
                if isinstance(parsed, dict):
                    entry = parsed.get("entry_point", "")
            except Exception:
                pass
        elif isinstance(fs_out, dict):
            entry = fs_out.get("entry_point", "")

    if not entry:
        return "ERROR: no entry point"

    cmd = f"{sys.executable} {entry} {args}".strip()
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": sandbox,
    }
    try:
        proc = subprocess.Popen(
            cmd, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=sandbox, env=env,
        )
    except Exception as e:
        return f"ERROR: failed to launch: {e}"

    try:
        proc.wait(timeout=3)
        stdout, stderr = proc.communicate()
        parts = [f"Command: {cmd}", f"exit_code: {proc.returncode}"]
        if stdout and stdout.strip():
            parts.append(f"stdout:\n{stdout.strip()[:2000]}")
        if stderr and stderr.strip():
            parts.append(f"stderr:\n{stderr.strip()[:2000]}")
        det_status, det_details = _verify_deterministic(tool_context)
        if det_status == "FAIL":
            parts.insert(0, f"STARTUP_FAILED_CODE:DETERMINISTIC_VERIFICATION_FAIL\n{det_details}")
        elif proc.returncode == 0:
            parts.insert(0, "STARTED_OK")
        else:
            category = _classify_failure(stderr or "", stdout or "")
            prefix = f"STARTUP_FAILED_ENV:{category}" if category in ("MISSING_ARGS", "DB", "NETWORK") else (
                f"STARTUP_FAILED_CODE:{category}" if category else "STARTUP_FAILED_CODE:UNKNOWN"
            )
            parts.insert(0, prefix)
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        det_status, det_details = _verify_deterministic(tool_context)
        if det_status == "FAIL":
            parts = [f"STARTUP_FAILED_CODE:DETERMINISTIC_VERIFICATION_FAIL\n{det_details}", f"Command: {cmd}"]
        else:
            parts = ["STARTED_OK", f"Command: {cmd}"]
        if stdout and stdout.strip():
            parts.append(f"stdout:\n{stdout.strip()[:2000]}")
        if stderr and stderr.strip():
            parts.append(f"stderr:\n{stderr.strip()[:2000]}")
        return "\n".join(parts)


def compare_original_and_modified(tool_context: ToolContext) -> str:
    """Compare each modified file against the original in the target codebase.

    Returns a unified diff for every modified file, plus a summary of which
    files have substantive changes. Only modified files (those listed in
    state["modified_files"]) are compared.

    The verifier should use this output to assess whether the optimizer's
    changes are correct — and only provide a suggestion back to the optimizer
    when a genuine issue is found.
    """
    target = tool_context.state.get("target", "")
    sandbox = tool_context.state.get("sandbox", "")
    modified = tool_context.state.get("modified_files", [])

    if not modified:
        return "No modified files to compare."

    if not target:
        return "ERROR: target directory not set in session state."

    sections = []
    for rel in modified:
        original = os.path.join(target, rel)
        updated = os.path.join(sandbox, rel)

        try:
            original_text = Path(original).read_text(errors="replace")
        except FileNotFoundError:
            sections.append(f"## {rel}\n(NEW FILE — no original to compare)\n")
            continue
        except Exception as e:
            sections.append(f"## {rel}\nERROR reading original: {e}\n")
            continue

        try:
            updated_text = Path(updated).read_text(errors="replace")
        except FileNotFoundError:
            sections.append(f"## {rel}\nERROR: modified file not found in sandbox\n")
            continue
        except Exception as e:
            sections.append(f"## {rel}\nERROR reading modified: {e}\n")
            continue

        original_lines = original_text.splitlines()
        updated_lines = updated_text.splitlines()

        if original_lines == updated_lines:
            sections.append(f"## {rel}\n(no changes)\n")
            continue

        diff = difflib.unified_diff(
            original_lines, updated_lines,
            fromfile=f"original/{rel}",
            tofile=f"sandbox/{rel}",
            lineterm="",
        )
        diff_text = "\n".join(diff)
        diff_text = diff_text[:5000]
        if len(diff_text) >= 5000:
            diff_text += "\n... (truncated)"

        lines_added = sum(1 for line in diff_text.split("\n") if line.startswith("+") and not line.startswith("+++"))
        lines_removed = sum(1 for line in diff_text.split("\n") if line.startswith("-") and not line.startswith("---"))

        sections.append(
            f"## {rel}\n"
            f"Lines added: {lines_added}, removed: {lines_removed}\n\n"
            f"```diff\n{diff_text}\n```\n"
        )

    if not sections:
        return "No modified files to compare."

    return "\n".join(sections)


def run_deterministic_verification(tool_context: ToolContext) -> str:
    """Run deterministic AST verification against rewrite contracts.
    
    Validates structural changes made by the optimizer using the AST logic,
    checking if N+1 database operations within loops were removed and replaced.
    """
    state = tool_context.state
    contracts_data = state.get("rewrite_contracts")

    if not contracts_data:
        return "No rewrite contracts in state."

    try:
        contracts = _deserialize_contracts(contracts_data)
    except Exception as e:
        return f"ERROR deserializing contracts: {e}"

    result = execute_deterministic_verification(
        state.get("target", ""),
        state.get("sandbox", ""),
        contracts,
        state.get("modified_files", []),
    )
    state["deterministic_verification"] = result.model_dump()

    lines = _format_verification_details(result)
    lines += ["```json", json.dumps(result.model_dump(), indent=2), "```"]
    return "\n".join(lines)

