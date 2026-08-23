"""Codebase copier — copies codebase into a sandbox directory and rewrites import paths."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path

from google.adk.tools import ToolContext

SANDBOX_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "output")

_CODE_EXTENSIONS = {".py", ".pyx", ".pyi"}


def copy_entire(source_root: str, sandbox_id: str | None = None) -> str:
    """Copy entire *source_root* into a sandbox."""
    sid = sandbox_id or uuid.uuid4().hex[:12]
    dest = os.path.join(SANDBOX_ROOT, sid)

    if os.path.exists(dest):
        shutil.rmtree(dest)

    shutil.copytree(source_root, dest, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".venv", "venv", "node_modules",
        "*.pyc", ".mypy_cache", ".pytest_cache", "sandbox", "output_sandbox", "output"
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


def read_files(root: str, file_paths: list[str]) -> dict[str, str]:
    """Read contents of *file_paths* from *root*. Returns {path: content}."""
    contents: dict[str, str] = {}
    for rel_path in file_paths:
        abs_path = os.path.join(root, rel_path)
        if not os.path.isfile(abs_path):
            continue
        try:
            contents[rel_path] = Path(abs_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return contents


def copy_to_sandbox(tool_context: ToolContext) -> str:
    """Copy the target codebase into a sandbox and rewrite import paths.

    Reads ``target`` from session state and writes the sandbox path back to
    state as ``sandbox``.
    """
    target = tool_context.state.get("target", "")
    if not target:
        return "ERROR: target path not set in state"
    sandbox = copy_entire(target)
    n = rewrite_imports(sandbox, target)
    tool_context.state["sandbox"] = sandbox
    return f"OK: sandbox created at {sandbox} ({n} files had imports rewritten)"
