"""Tools for the file selector agent — retrieve project file listing."""
from __future__ import annotations

import os
from typing import Union

from google.adk.tools import ToolContext
from src.intent_analyzer.tools.db_engine import (
    engine_constraint_note,
    filter_paths_by_db_type,
)
from src.intent_analyzer.tools.scanner import scan_directory


def _apply_db_type(listing: Union[list[str], str], db_type: str) -> Union[list[str], str]:
    """Apply the target-engine constraint to a project listing.

    When ``db_type`` is falsy the listing is returned exactly as-is. When set,
    foreign-engine paths are filtered from list listings and the result is
    returned as a constraint-prefixed string.
    """
    if not db_type:
        return listing
    note = engine_constraint_note(db_type)
    header = f"{note}\n\n## Project listing\n"
    if isinstance(listing, list):
        filtered = filter_paths_by_db_type(listing, db_type)
        return header + "\n".join(filtered)
    return header + listing


def get_project_files(tool_context: ToolContext) -> Union[list[str], str]:
    """Retrieve the real project file listing from session state or by scanning the target directory.

    Checks session state for ``scan_result`` (or ``file_list``). If present, returns
    the file listing. Otherwise, if ``target`` is present in session state, scans
    the directory using ``scan_directory`` and returns the list of file paths.

    When a ``db_type`` is present in session state, the listing is constrained to
    the requested database engine (foreign-engine files are dropped).
    """
    db_type = tool_context.state.get("db_type", "")

    if "scan_result" in tool_context.state and tool_context.state["scan_result"]:
        scan_res = tool_context.state["scan_result"]
        if isinstance(scan_res, (list, str)):
            return _apply_db_type(scan_res, db_type)
        if hasattr(scan_res, "files"):
            listing = [f.relative_path if hasattr(f, "relative_path") else str(f) for f in scan_res.files]
            return _apply_db_type(listing, db_type)
        if isinstance(scan_res, dict) and "files" in scan_res:
            return _apply_db_type(scan_res["files"], db_type)
        return _apply_db_type(str(scan_res), db_type)

    if "file_list" in tool_context.state and tool_context.state["file_list"]:
        return _apply_db_type(tool_context.state["file_list"], db_type)

    target = tool_context.state.get("target", "")
    if target:
        if os.path.isdir(target):
            return _apply_db_type(scan_directory(target), db_type)
        return f"ERROR: Target directory '{target}' does not exist or is not a directory."

    return "ERROR: Neither 'scan_result' nor 'target' found in state."
