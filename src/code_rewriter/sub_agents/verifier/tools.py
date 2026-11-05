"""Tools for the verifier agent — syntax check, compare original vs modified, run application."""

import ast
import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from google.adk.tools import ToolContext

from src.code_rewriter._common import extract_function_source_by_name as _extract_function_source_by_name
from src.code_rewriter.models.rewrite_models import RewriteContract
from src.code_rewriter.tools.pipeline_analysis import (
    verify_all_contracts,
    verify_contract_target,
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

_SHELL_METACHARACTERS = frozenset(";|&$`<>(){}\n\r\x00")
_MAX_ENTRY_LENGTH = 1024
_MAX_ARGS_LENGTH = 4096

_ENTRY_CANDIDATES = ("__main__.py", "main.py", "app.py", "manage.py", "cli.py")
_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    "tests", "test", ".tox", ".mypy_cache", ".pytest_cache", "build", "dist",
})
_SKIP_FILES = frozenset({"setup.py", "conftest.py"})
_MAX_PROBED_ENTRIES = 5
_MAX_SMOKE_MODULES = 20
_MAX_SMOKE_LINE = 300
_SMOKE_TIMEOUT = 10


def _resolve_entry_path(sandbox: str, entry: str) -> str | None:
    """Resolve *entry* to an existing file confined to *sandbox*.

    Returns the absolute path when it is safe to execute, or ``None`` when the
    entry is empty, oversized, contains shell metacharacters, escapes the
    sandbox, or does not reference an existing file.
    """
    if not sandbox or not entry or len(entry) > _MAX_ENTRY_LENGTH:
        return None
    if any(ch in _SHELL_METACHARACTERS for ch in entry):
        return None
    sandbox_real = os.path.realpath(sandbox)
    candidate = os.path.realpath(os.path.join(sandbox_real, entry))
    if candidate != sandbox_real and not candidate.startswith(sandbox_real + os.sep):
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate


def _split_application_args(args: str) -> list[str] | None:
    """Split *args* into an argv tail without involving a shell.

    Returns an empty list for empty input, or ``None`` when the argument string
    is oversized or cannot be parsed.
    """
    if not args:
        return []
    if len(args) > _MAX_ARGS_LENGTH:
        return None
    try:
        return shlex.split(args)
    except ValueError:
        return None


def _declared_entry(state) -> str:
    """Read ``file_selector_output.entry_point`` from session state (may be empty)."""
    if not state:
        return ""
    fs_out = state.get("file_selector_output", {})
    if hasattr(fs_out, "entry_point"):
        return fs_out.entry_point or ""
    if isinstance(fs_out, str):
        try:
            parsed = json.loads(fs_out)
        except Exception:
            return ""
        return parsed.get("entry_point", "") if isinstance(parsed, dict) else ""
    if isinstance(fs_out, dict):
        entry = fs_out.get("entry_point", "")
        return entry if isinstance(entry, str) else ""
    return ""


def _module_name(sandbox: str, rel: str) -> str:
    """Derive a dotted import name for *rel*, walking up through ``__init__.py`` packages."""
    rel = rel.replace(os.sep, "/").lstrip("./")
    parts = [Path(rel).stem]
    parent = os.path.dirname(rel)
    while parent and os.path.isfile(os.path.join(sandbox, parent, "__init__.py")):
        parts.insert(0, os.path.basename(parent))
        parent = os.path.dirname(parent)
    if len(parts) > 1 and parts[-1] == "__init__":
        parts.pop()  # a package's __init__.py is imported as the package itself
    return ".".join(parts)


def _import_ok(sandbox: str, rel: str) -> bool:
    """Cheap probe: can *rel* be imported as a module from *sandbox*?"""
    module = _module_name(sandbox, rel)
    if not module:
        return False
    try:
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=sandbox, env=_sandbox_env(sandbox), capture_output=True,
            text=True, timeout=_SMOKE_TIMEOUT, shell=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _is_main_guard(node: ast.AST) -> bool:
    """True when *node* is ``if __name__ == "__main__":``."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False

    def _is_name(n):
        return isinstance(n, ast.Name) and n.id == "__name__"

    def _is_main(n):
        return isinstance(n, ast.Constant) and n.value == "__main__"

    left, right = test.left, test.comparators[0]
    return (_is_name(left) and _is_main(right)) or (_is_name(right) and _is_main(left))


def _has_main_guard(path: str) -> bool:
    """True when *path* parses and has a top-level ``__main__`` guard. Never raises."""
    try:
        tree = ast.parse(Path(path).read_text(errors="replace"))
    except Exception:
        return False
    return any(_is_main_guard(node) for node in tree.body)


def _pyproject_scripts(sandbox: str) -> list[str]:
    """Resolve ``[project.scripts]`` ``module:func`` targets to sandbox-relative .py paths."""
    pyproject = os.path.join(sandbox, "pyproject.toml")
    try:
        data = tomllib.loads(Path(pyproject).read_text(errors="replace"))
    except Exception:
        return []
    scripts = (data.get("project") or {}).get("scripts") or {}
    resolved = []
    for target in scripts.values():
        if not isinstance(target, str):
            continue
        module = target.split(":", 1)[0].strip()
        if not module:
            continue
        base = module.replace(".", "/")
        for rel in (f"{base}.py", f"{base}/__init__.py"):
            if os.path.isfile(os.path.join(sandbox, rel)):
                resolved.append(rel)
                break
    return resolved


def _discover_entry(sandbox: str, declared: str) -> tuple[str, str]:
    """Find a runnable entry point inside *sandbox*.

    Returns ``(entry_rel_path, source)`` with ``source`` in ``declared`` |
    ``discovered`` | ``none``. Deterministic order: the *declared* entry, then
    ``__main__.py``, ``main.py``, ``app.py``, ``manage.py``, ``cli.py``, then any
    ``*.py`` with an ``if __name__ == "__main__":`` guard (sorted by path), then
    ``[project.scripts]`` from ``pyproject.toml``. Never raises.
    """
    if not sandbox or not os.path.isdir(sandbox):
        return "", "none"
    if declared and _resolve_entry_path(sandbox, declared):
        return declared, "declared"

    for name in _ENTRY_CANDIDATES:
        if _resolve_entry_path(sandbox, name):
            return name, "discovered"

    guarded: list[str] = []
    for root, dirs, files in os.walk(sandbox):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for filename in sorted(files):
            if not filename.endswith(".py") or filename in _SKIP_FILES:
                continue
            full = os.path.join(root, filename)
            if _has_main_guard(full):
                rel = os.path.relpath(full, sandbox)
                if _resolve_entry_path(sandbox, rel):
                    guarded.append(rel)
    guarded.sort()
    # ponytail: probe at most a few, prefer one that imports (a module with a
    # missing third-party dep is not the runnable app entry). Bump the cap if
    # discovery keeps picking a dead script in a repo with many __main__ files.
    for rel in guarded[:_MAX_PROBED_ENTRIES]:
        if _import_ok(sandbox, rel):
            return rel, "discovered"
    if guarded:
        return guarded[0], "discovered"

    for rel in _pyproject_scripts(sandbox):
        if _resolve_entry_path(sandbox, rel):
            return rel, "discovered"

    return "", "none"


def _sandbox_env(sandbox: str) -> dict[str, str]:
    """Environment for running sandbox code without leaking bytecode or host paths."""
    return {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": sandbox,
    }


def _import_smoke(sandbox: str, modified_files: list[str]) -> tuple[str, str]:
    """Import each modified Python module; returns ``(status, details)``.

    ``status`` is ``OK``, ``FAILED`` (any traceback) or ``SKIPPED`` (nothing to
    import). Never raises.
    """
    py_files = [f for f in modified_files or [] if str(f).endswith(".py")]
    if not sandbox or not os.path.isdir(sandbox) or not py_files:
        return "SKIPPED", "no modified Python files"

    failures: list[str] = []
    env = _sandbox_env(sandbox)
    for rel in py_files[:_MAX_SMOKE_MODULES]:
        if not os.path.isfile(os.path.join(sandbox, rel)):
            continue
        module = _module_name(sandbox, rel)
        if not module:
            continue
        try:
            proc = subprocess.run(
                [sys.executable, "-c", f"import {module}"],
                cwd=sandbox, env=env, capture_output=True, text=True,
                timeout=_SMOKE_TIMEOUT, shell=False,
            )
        except subprocess.TimeoutExpired:
            failures.append(f"{module}: timeout after {_SMOKE_TIMEOUT}s")
            continue
        except Exception as e:
            failures.append(f"{module}: {e}")
            continue
        if proc.returncode == 0:
            continue
        stderr = (proc.stderr or "").strip()
        if "Traceback" in stderr:
            err_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
            signature = err_lines[-1] if err_lines else "unknown error"
            failures.append(f"{module}: {signature[:_MAX_SMOKE_LINE]}")
        else:
            failures.append(f"{module}: exited {proc.returncode}")

    if not failures:
        return "OK", ", ".join(py_files[:_MAX_SMOKE_MODULES])
    return "FAILED", "; ".join(failures)[:1000]


def _record_smoke(state, entry: str, source: str, startup: str, import_status: str) -> None:
    """Publish smoke evidence for the pipeline's result.json. Never raises."""
    if state is None:
        return
    try:
        state["smoke_evidence"] = {
            "entry": entry,
            "entry_source": source,
            "startup": startup,
            "import": import_status,
        }
    except Exception:
        pass


def _import_smoke_line(status: str, details: str) -> str:
    """Render the import-smoke evidence as one output line."""
    if status == "FAILED":
        return f"Import smoke: FAILED ({details})"
    return f"Import smoke: {status}"


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


def _current_contract(state) -> RewriteContract | None:
    """Parse ``state["current_contract"]`` (dict or model). Returns None if absent/invalid."""
    raw = state.get("current_contract")
    if not raw:
        return None
    if isinstance(raw, RewriteContract):
        return raw
    try:
        if isinstance(raw, dict):
            return RewriteContract(**raw)
        return RewriteContract.model_validate(raw)
    except Exception:
        return None


def _run_contract_verification(tool_context: ToolContext | None) -> tuple[str, str | None]:
    """Run deterministic contract verification if contracts exist in tool_context.state.

    In IN-LOOP mode (``state["current_contract"]`` set) only that target is
    verified — other targets are still unrewritten and must not fail the app
    check. Otherwise every contract is verified.

    Returns (status, details) where status is 'PASS', 'FAIL', or 'NONE' (if no contracts).
    """
    if not tool_context or not hasattr(tool_context, "state") or not tool_context.state:
        return "NONE", None

    state = tool_context.state
    target = state.get("target", "")
    sandbox = state.get("sandbox", "")

    current = _current_contract(state)
    if current is not None:
        result = verify_contract_target(target, sandbox, current)
    else:
        contracts_data = state.get("rewrite_contracts")
        if not contracts_data:
            return "NONE", None
        try:
            contracts = _deserialize_contracts(contracts_data)
        except Exception:
            return "NONE", None
        result = verify_all_contracts(
            target,
            sandbox,
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


def _launch_once(
    entry_path: str,
    entry_rel: str,
    entry_source: str,
    sandbox: str,
    arg_tokens: list[str],
    tool_context: ToolContext | None,
) -> tuple[str, list[str]]:
    """Launch the app once and classify the outcome.

    Returns ``(first_line_status, parts)`` where ``parts[0]`` is the status block
    (multi-line for deterministic failures) and the rest is command/exit/output
    evidence. Deterministic verification always takes precedence.
    """
    argv = [sys.executable, entry_path, *arg_tokens]
    cmd = shlex.join(argv)
    try:
        proc = subprocess.Popen(
            argv, shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=sandbox, env=_sandbox_env(sandbox),
        )
    except Exception as e:
        status = "STARTUP_FAILED_CODE:UNKNOWN"
        return status, [
            f"ERROR: failed to launch: {e}",
            f"Command: {cmd}",
            f"Entry: {entry_rel} ({entry_source})",
        ]

    try:
        proc.wait(timeout=3)
        stdout, stderr = proc.communicate()
        parts = [
            f"Command: {cmd}",
            f"Entry: {entry_rel} ({entry_source})",
            f"exit_code: {proc.returncode}",
        ]
        if stdout and stdout.strip():
            parts.append(f"stdout:\n{stdout.strip()[:2000]}")
        if stderr and stderr.strip():
            parts.append(f"stderr:\n{stderr.strip()[:2000]}")
        det_status, det_details = _run_contract_verification(tool_context)
        if det_status == "FAIL":
            status = f"STARTUP_FAILED_CODE:DETERMINISTIC_VERIFICATION_FAIL\n{det_details}"
        elif proc.returncode == 0:
            status = "STARTED_OK"
        else:
            category = _classify_failure(stderr or "", stdout or "")
            status = f"STARTUP_FAILED_ENV:{category}" if category in ("MISSING_ARGS", "DB", "NETWORK") else (
                f"STARTUP_FAILED_CODE:{category}" if category else "STARTUP_FAILED_CODE:UNKNOWN"
            )
        return status.splitlines()[0], [status, *parts]
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        det_status, det_details = _run_contract_verification(tool_context)
        if det_status == "FAIL":
            status = f"STARTUP_FAILED_CODE:DETERMINISTIC_VERIFICATION_FAIL\n{det_details}"
            parts = [status, f"Command: {cmd}"]
        else:
            status = "STARTED_OK"
            parts = [status, f"Command: {cmd}"]
        parts.append(f"Entry: {entry_rel} ({entry_source})")
        if stdout and stdout.strip():
            parts.append(f"stdout:\n{stdout.strip()[:2000]}")
        if stderr and stderr.strip():
            parts.append(f"stderr:\n{stderr.strip()[:2000]}")
        return status.splitlines()[0], parts


def run_application(args: str = "", tool_context: ToolContext | None = None) -> str:
    """Launch the application and verify it starts without an immediate crash.

    Non-blocking startup check: the application is launched with Popen and only
    watched for a short window (3 seconds). It is NOT run to completion.

    Behavior:
    - The entry point is taken from `file_selector_output.entry_point` when it
      resolves; otherwise it is discovered deterministically inside the sandbox
      (`__main__.py`, `main.py`, `app.py`, `manage.py`, `cli.py`, then files with
      an `if __name__ == "__main__":` guard, then `[project.scripts]`).
    - When no entry point exists at all, "NO_ENTRY_POINT" is returned instead of
      an error. That is not a code failure — there is simply nothing to launch.
    - Every modified Python module is import-smoked (a real `import` with the
      sandbox on PYTHONPATH) and the result is reported as
      `Import smoke: OK|FAILED …`, regardless of whether the app started.
    - If the process is still running after 3 seconds, it started cleanly
      without an immediate crash -> it is terminated and "STARTED_OK" is
      returned along with the command and any early stdout/stderr.
    - If the process exited within 3 seconds with returncode 0, "STARTED_OK"
      is returned along with the captured output.
    - If rewrite_contracts exist in state and fail deterministic AST
      verification, "STARTUP_FAILED_CODE:DETERMINISTIC_VERIFICATION_FAIL"
      is returned instead of "STARTED_OK". In IN-LOOP mode (a non-empty
      `current_contract` in state) only that contract is verified; otherwise
      all contracts are.
    - If the process exited within 3 seconds with a non-zero returncode, the
      failure is classified into one of:

      * ``STARTUP_FAILED_ENV:MISSING_ARGS`` — the app needs CLI arguments
        (usage message, argument required error).  This is NOT a code error.
        When the caller passed no arguments, the tool itself relaunches once
        with ``--help`` and reports the retry's final status; the verifier must
        not retry again.
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
        args: Optional CLI arguments to pass. Leave empty to let the tool pick
              the arguments (it retries once with ``--help`` when the app
              complains about missing arguments). When args are given they are
              used verbatim and no automatic retry happens.
    """
    state = getattr(tool_context, "state", None) if tool_context is not None else None
    sandbox = (state.get("sandbox", "") if state else "") or ""
    modified = list(state.get("modified_files") or []) if state else []

    entry_rel, entry_source = _discover_entry(sandbox, _declared_entry(state))
    import_status, import_details = _import_smoke(sandbox, modified)
    smoke_line = _import_smoke_line(import_status, import_details)

    def _finish(startup: str, lines: list[str]) -> str:
        _record_smoke(state, entry_rel, entry_source, startup, import_status)
        return "\n".join(lines)

    if not entry_rel:
        return _finish("NO_ENTRY_POINT", [
            "NO_ENTRY_POINT",
            "No runnable entry point found in the sandbox (searched __main__.py, main.py, "
            "app.py, manage.py, cli.py, __main__ guards, [project.scripts]).",
            smoke_line,
            "This is not a code defect: there is nothing to launch. Judge the change on "
            "deterministic verification, syntax and import results.",
        ])

    arg_tokens = _split_application_args(args)
    if arg_tokens is None:
        return _finish("STARTUP_FAILED_CODE:UNKNOWN", [
            "ERROR: invalid application arguments",
            f"Entry: {entry_rel} ({entry_source})",
            smoke_line,
        ])

    entry_path = _resolve_entry_path(sandbox, entry_rel)
    if not entry_path:
        return _finish("NO_ENTRY_POINT", [
            "NO_ENTRY_POINT",
            f"Resolved entry {entry_rel!r} is unsafe or missing.",
            smoke_line,
        ])

    status, parts = _launch_once(entry_path, entry_rel, entry_source, sandbox, arg_tokens, tool_context)

    if status == "STARTUP_FAILED_ENV:MISSING_ARGS" and not arg_tokens:
        # Deterministic single retry: the app asked for arguments, so try --help
        # once instead of leaving it to the LLM (which does it inconsistently).
        retry_status, retry_parts = _launch_once(
            entry_path, entry_rel, entry_source, sandbox, ["--help"], tool_context,
        )
        parts = [
            retry_parts[0],
            "first attempt: MISSING_ARGS",
            *parts[1:],
            "retry: --help",
            *retry_parts[1:],
        ]
        status = retry_status

    parts.append(smoke_line)
    return _finish(status, parts)


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


def get_verification_context(tool_context: ToolContext) -> str:
    """Return per-target contract, analysis, and original/optimized source.

    For every rewrite contract in session state, this renders the contract to
    comply with, the AST analysis summary, the original function source, and the
    optimized function source extracted from the sandbox (when present).
    """
    state = tool_context.state
    contracts_data = state.get("rewrite_contracts")
    if not contracts_data:
        return "No rewrite contracts in state."

    current_contract = state.get("current_contract")
    if isinstance(current_contract, dict) and current_contract:
        cc_target = current_contract.get("target") or {}
        cc_file = cc_target.get("file", "")
        cc_fn = cc_target.get("qualified_function") or cc_target.get("function") or ""
        scoped = []
        for contract in contracts_data:
            if isinstance(contract, dict):
                cd = contract
            elif hasattr(contract, "model_dump"):
                cd = contract.model_dump()
            else:
                continue
            t = cd.get("target") or {}
            fn = t.get("qualified_function") or t.get("function") or ""
            if t.get("file") == cc_file and fn == cc_fn:
                scoped.append(contract)
        contracts_data = scoped

    ctx_map = state.get("target_context_map") or {}
    sandbox = state.get("sandbox", "")

    def _fmt(value: object) -> str:
        if isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value) if value else "(none)"
        return str(value) if value not in (None, "") else "(none)"

    sections: list[str] = []
    for contract in contracts_data:
        if isinstance(contract, dict):
            c = contract
        elif hasattr(contract, "model_dump"):
            c = contract.model_dump()
        else:
            continue

        t = c.get("target") or {}
        file = t.get("file", "")
        qfn = t.get("qualified_function") or t.get("function") or ""

        ctx = ctx_map.get(qfn, {}) or {}
        analysis_summary = ctx.get("analysis_summary") or "(unavailable)"
        original_source = ctx.get("function_source") or "(unavailable)"

        optimized_source = "(not modified or missing)"
        if sandbox and file:
            opt_path = os.path.join(sandbox, file)
            if os.path.exists(opt_path):
                try:
                    opt_src_text = Path(opt_path).read_text(errors="replace")
                except Exception:
                    opt_src_text = ""
                extracted = _extract_function_source_by_name(opt_src_text, qfn)
                if extracted:
                    optimized_source = extracted

        section = (
            f"## Target: {qfn} ({file})\n"
            f"### Contract\n"
            f"- Rewrite ID: {_fmt(c.get('rewrite_id'))}\n"
            f"- Pattern: {_fmt(c.get('pattern'))}\n"
            f"- Strategy: {_fmt(c.get('strategy'))}\n"
            f"- Allowed edit regions: {_fmt(c.get('allowed_regions'))}\n"
            f"- Must preserve: {_fmt(c.get('must_preserve'))}\n"
            f"- Must not change: {_fmt(c.get('must_not_change'))}\n"
            f"### Analysis Summary\n"
            f"{analysis_summary}\n"
            f"### Original Function Source\n"
            f"```python\n{original_source}\n```\n"
            f"### Optimized Function Source\n"
            f"```python\n{optimized_source}\n```"
        )
        sections.append(section)

    return "\n\n".join(sections)


def run_contract_verification(tool_context: ToolContext) -> str:
    """Run deterministic contract verification against rewrite contracts.

    Validates structural changes made by the optimizer using the AST logic,
    checking if N+1 database operations within loops were removed and replaced.
    """
    state = tool_context.state
    contracts_data = state.get("rewrite_contracts")

    if not contracts_data:
        return "No rewrite contracts in state."

    current_contract = state.get("current_contract")
    if isinstance(current_contract, dict) and current_contract:
        try:
            contract = RewriteContract(**current_contract)
        except Exception as e:
            return f"ERROR deserializing current_contract: {e}"

        result = verify_contract_target(
            state.get("target", ""),
            state.get("sandbox", ""),
            contract,
        )
    else:
        try:
            contracts = _deserialize_contracts(contracts_data)
        except Exception as e:
            return f"ERROR deserializing contracts: {e}"

        result = verify_all_contracts(
            state.get("target", ""),
            state.get("sandbox", ""),
            contracts,
            state.get("modified_files", []),
        )
    state["deterministic_verification"] = result.model_dump()

    lines = _format_verification_details(result)
    lines += ["```json", json.dumps(result.model_dump(), indent=2), "```"]
    return "\n".join(lines)

