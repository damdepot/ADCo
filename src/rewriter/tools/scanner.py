"""Directory scanner — walks a codebase and returns structure + file metadata."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from google.adk.tools import ToolContext


@dataclass
class FileInfo:
    relative_path: str
    absolute_path: str
    extension: str
    size_bytes: int


@dataclass
class ScanResult:
    root: str
    files: list[FileInfo] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)

    def package_name(self) -> str:
        return os.path.basename(self.root.rstrip("/"))


IGNORE_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", ".tox", "dist", "build",
    "sandbox", "output_sandbox", "output", ".env", "egg-info",
}

IGNORE_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll",
    ".db", ".sqlite", ".sqlite3",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".ttf", ".woff", ".woff2", ".eot",
    ".zip", ".tar", ".gz", ".bz2", ".7z",
    ".lock", ".log",
}


def scan(root: str, *, ignore_dirs: set[str] | None = None, ignore_exts: set[str] | None = None) -> ScanResult:
    """Walk *root* and build a `ScanResult` with file metadata and previews."""
    root_path = Path(root).resolve()
    ignore_d = IGNORE_DIRS | (ignore_dirs or set())
    ignore_x = IGNORE_EXTENSIONS | (ignore_exts or set())

    result = ScanResult(root=str(root_path))

    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in ignore_d]

        rel_dir = os.path.relpath(dirpath, root_path)
        if rel_dir != "." and rel_dir not in result.dirs:
            result.dirs.append(rel_dir)

        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext in ignore_x:
                continue

            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, root_path)
            size = os.path.getsize(abs_path)

            result.files.append(FileInfo(
                relative_path=rel_path,
                absolute_path=abs_path,
                extension=ext,
                size_bytes=size,
            ))

    return result


def scan_codebase(tool_context: ToolContext) -> str:
    """Scan the target codebase directory structure and return a file listing.

    Reads the target path from session state and stores the formatted listing
    in state as ``scan_result``.
    """
    target = tool_context.state.get("target", "")
    if not target:
        return "ERROR: target path not set in state"
    result = scan(target)
    listing = format_for_llm(result)
    tool_context.state["scan_result"] = listing
    return listing


def format_for_llm(result: ScanResult, max_files: int = 200) -> str:
    """Format a ScanResult for LLM consumption — compact, token-efficient."""
    lines = [f"# Project: {result.package_name()}", f"Root: {result.root}", ""]
    lines.append("## Directory tree")
    for d in sorted(result.dirs):
        lines.append(f"  {d}/")
    for f in result.files[:max_files]:
        lines.append(f"  {f.relative_path}")
    if len(result.files) > max_files:
        lines.append(f"  ... +{len(result.files) - max_files} more files")
    return "\n".join(lines)
