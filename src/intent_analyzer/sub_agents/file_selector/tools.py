"""Tools for the file selector agent — retrieve project file listing."""
from __future__ import annotations

import os
from typing import Union

from google.adk.tools import ToolContext
from src.intent_analyzer.tools.scanner import scan_directory


def get_project_files(tool_context: ToolContext) -> Union[list[str], str]:
    """Retrieve the real project file listing from session state or by scanning the target directory.

    Checks session state for ``scan_result`` (or ``file_list``). If present, returns
    the file listing. Otherwise, if ``target`` is present in session state, scans
    the directory using ``scan_directory`` and returns the list of file paths.
    """
    if "scan_result" in tool_context.state and tool_context.state["scan_result"]:
        scan_res = tool_context.state["scan_result"]
        if isinstance(scan_res, (list, str)):
            return scan_res
        if hasattr(scan_res, "files"):
            return [f.relative_path if hasattr(f, "relative_path") else str(f) for f in scan_res.files]
        if isinstance(scan_res, dict) and "files" in scan_res:
            return scan_res["files"]
        return str(scan_res)

    if "file_list" in tool_context.state and tool_context.state["file_list"]:
        return tool_context.state["file_list"]

    target = tool_context.state.get("target", "")
    if target:
        if os.path.isdir(target):
            return scan_directory(target)
        return f"ERROR: Target directory '{target}' does not exist or is not a directory."

    return "ERROR: Neither 'scan_result' nor 'target' found in state."
