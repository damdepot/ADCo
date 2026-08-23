"""Tools for the verifier agent — syntax check, compare original vs modified, run application."""

import difflib
import os
import re
import subprocess
import sys
from pathlib import Path
from google.adk.tools import ToolContext

_CODE_ERRORS = re.compile(
    r"SyntaxError|ImportError|ModuleNotFoundError|NameError|"
    r"AttributeError|TypeError|IndentationError|ValueError"
)
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
    if _CODE_ERRORS.search(combined):
        return "CODE"
    return None


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
    if tool_context:
        entry = tool_context.state.get("file_selector_output", {}).get("entry_point", "")
    else:
        entry = ""

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
        if proc.returncode == 0:
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
