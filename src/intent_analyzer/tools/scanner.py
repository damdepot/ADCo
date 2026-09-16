"""Scanner tool — scans directory and builds structured file listing."""
from __future__ import annotations

import os
from google.adk.tools import ToolContext

IGNORE_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    "dist",
    "build",
    ".eggs",
    "sandbox",
    "output_sandbox",
    "out",
    ".env",
}

IGNORE_FILES = {
    ".DS_Store",
    "uv.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
}


def scan_directory(root: str) -> list[str]:
    """Recursively scan directory and return relative paths of all relevant files."""
    file_list = []
    root_abs = os.path.abspath(root)

    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.endswith(".egg-info")]

        for f in filenames:
            if f in IGNORE_FILES or f.endswith((".pyc", ".pyo", ".pyd")):
                continue
            full_path = os.path.join(dirpath, f)
            rel_path = os.path.relpath(full_path, root_abs)
            file_list.append(rel_path)

    return sorted(file_list)


def scan_codebase(tool_context: ToolContext) -> str:
    """Scan the target codebase directory and store file listing in state.

    Reads ``target`` from ``tool_context.state``, recursively walks the
    directory (skipping venv, .git, caches, etc.), and returns a formatted
    file listing.
    """
    target = tool_context.state.get("target", "")
    if not target:
        return "ERROR: target path not set in state"
    if not os.path.isdir(target):
        return f"ERROR: target is not a directory: {target}"

    files = scan_directory(target)
    tool_context.state["file_list"] = files
    return f"Scanned {len(files)} files:\n" + "\n".join(f"  {f}" for f in files)
