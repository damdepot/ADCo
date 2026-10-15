"""Codebase copier — copies codebase into a sandbox directory and rewrites import paths."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path

from google.adk.tools import ToolContext

SANDBOX_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "out")

_CODE_EXTENSIONS = {".py", ".pyx", ".pyi"}

_SANDBOX_MARKER_NAME = ".adco_sandbox"
_SANDBOX_MARKER_CONTENT = "adco-managed-sandbox-v1"


class SandboxSafetyError(RuntimeError):
    """Raised when a sandbox destination is unsafe to delete."""


def _is_empty_dir(path: str) -> bool:
    """True when *path* is a directory with no entries."""
    try:
        with os.scandir(path) as entries:
            return not any(entries)
    except OSError:
        return False


def _is_managed_sandbox(dest: str) -> bool:
    """True when *dest* contains the marker written by this module."""
    marker = os.path.join(dest, _SANDBOX_MARKER_NAME)
    try:
        content = Path(marker).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return content == _SANDBOX_MARKER_CONTENT


def _protected_paths() -> list[str]:
    """Paths that must never be removed by sandbox recreation."""
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    return [project_root, os.getcwd(), os.path.abspath(SANDBOX_ROOT)]


def _contains_protected_path(dest: str) -> str | None:
    """Return a protected path that *dest* equals or contains, if any."""
    dest_real = os.path.realpath(dest)
    for candidate in _protected_paths():
        candidate_real = os.path.realpath(candidate)
        if dest_real == candidate_real:
            return candidate_real
        try:
            if os.path.commonpath([dest_real, candidate_real]) == dest_real:
                return candidate_real
        except ValueError:
            continue
    return None


def copy_entire(source_root: str, dest_override: str | None = None) -> str:
    """Copy entire *source_root* into a sandbox.

    An existing destination is only replaced when it is an empty directory or a
    sandbox previously created by this module (recognised by the
    ``.adco_sandbox`` marker file). Any other existing directory is left
    untouched and a :class:`SandboxSafetyError` is raised instead.
    """
    if dest_override:
        dest = dest_override
    else:
        dest = os.path.join(SANDBOX_ROOT, uuid.uuid4().hex[:12])

    dest = os.path.abspath(dest)

    if os.path.exists(dest):
        if not os.path.isdir(dest):
            raise SandboxSafetyError(
                f"refusing to overwrite non-directory sandbox path: {dest}"
            )
        if not _is_empty_dir(dest):
            if not _is_managed_sandbox(dest):
                raise SandboxSafetyError(
                    f"refusing to delete existing directory {dest!r}: it is not a "
                    f"recognised ADCo sandbox (no {_SANDBOX_MARKER_NAME} marker). "
                    f"Remove it manually or choose a fresh sandbox directory."
                )
            blocked = _contains_protected_path(dest)
            if blocked:
                raise SandboxSafetyError(
                    f"refusing to delete {dest!r}: it contains the protected path {blocked!r}"
                )
            shutil.rmtree(dest)

    os.makedirs(dest, exist_ok=True)
    Path(os.path.join(dest, _SANDBOX_MARKER_NAME)).write_text(
        _SANDBOX_MARKER_CONTENT, encoding="utf-8"
    )
    shutil.copytree(source_root, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".venv", "venv", "node_modules",
        "*.pyc", ".mypy_cache", ".pytest_cache", "sandbox", "output_sandbox", "out",
        _SANDBOX_MARKER_NAME,
    ))

    return os.path.abspath(dest)


def _project_root(source_root: str) -> str | None:
    """Walk up from *source_root* to find the nearest project root.

    Detected by the presence of ``pyproject.toml``, ``setup.py`` or ``.git``.
    Returns the project root path, or ``None`` if none is found.
    """
    cur = os.path.abspath(source_root)
    while cur and cur != os.path.dirname(cur):
        for marker in ("pyproject.toml", "setup.py", ".git"):
            if os.path.exists(os.path.join(cur, marker)):
                return cur
        cur = os.path.dirname(cur)
    return None


def rewrite_imports(sandbox: str, source_root: str) -> int:
    """Rewrite import paths in sandbox files to match the flattened layout.

    The codebase at *source_root* is copied flat into the sandbox root, so the
    old package path no longer applies. The following prefixes are stripped from
    import statements and ``__import__`` calls:

    1. The dotted path relative to the project root (e.g. ``pkg.sub``) —
       detected by walking up to the nearest ``pyproject.toml`` /
       ``setup.py`` / ``.git``.
    2. The package's own name (the basename of *source_root*).

    Rewrites are applied to ``from PKG import X``, ``from PKG.sub import Y``,
    ``import PKG.sub as Z``, ``import PKG.sub``, and
    ``__import__('PKG.sub....')``.

    Returns the number of files modified.
    """
    prefixes: list[str] = []
    proj = _project_root(source_root)
    if proj:
        rel = os.path.relpath(os.path.abspath(source_root), proj)
        if rel and rel != ".":
            full_prefix = rel.replace(os.sep, ".")
            if full_prefix:
                prefixes.append(full_prefix)
    pkg_name = os.path.basename(os.path.normpath(source_root))
    if pkg_name and pkg_name not in prefixes:
        prefixes.append(pkg_name)

    if not prefixes:
        return 0

    # Build regexes for each prefix: dotted-form and bare-form.
    # dotted:  from/import PKG.sub  ->  from/import sub
    #          __import__('PKG.sub  ->  __import__('sub
    # bare:    from PKG import X    ->  import X
    patterns: list[tuple[re.Pattern, str]] = []
    for p in prefixes:
        patterns.extend([
            (re.compile(r'\b(from|import)\s+' + re.escape(p) + r'\.'), r'\1 '),
            (re.compile(r"__import__\(\s*'" + re.escape(p) + r'\.'), r"__import__('"),
            (re.compile(r'__import__\(\s*"' + re.escape(p) + r'\.'), r'__import__("'),
            (re.compile(r'\bfrom\s+' + re.escape(p) + r'\s+import\b'), r'import'),
        ])

    count = 0
    for root, _, files in os.walk(sandbox):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _CODE_EXTENSIONS:
                continue
            full = os.path.join(root, fname)
            text = Path(full).read_text(encoding="utf-8", errors="replace")
            new_text = text
            total_n = 0
            for pat, repl in patterns:
                new_text, n = pat.subn(repl, new_text)
                total_n += n
            if total_n > 0:
                Path(full).write_text(new_text, encoding="utf-8")
                count += 1

    return count


def copy_to_sandbox(tool_context: ToolContext) -> str:
    """Copy the target codebase into a sandbox and rewrite import paths.

    Reads ``target`` from session state and writes the sandbox path back to
    state as ``sandbox``.
    """
    target = tool_context.state.get("target", "")
    if not target:
        return "ERROR: target path not set in state"
    dest_override = tool_context.state.get("sandbox_dir") or tool_context.state.get("dest_override")
    try:
        sandbox = copy_entire(target, dest_override=dest_override)
    except SandboxSafetyError as e:
        return f"ERROR: {e}"
    n = rewrite_imports(sandbox, target)
    tool_context.state["sandbox"] = sandbox
    return f"OK: sandbox created at {sandbox} ({n} files had imports rewritten)"
