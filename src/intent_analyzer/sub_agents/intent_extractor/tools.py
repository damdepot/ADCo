"""Tools for the intent extractor agent — read selected files."""
import os
from google.adk.tools import ToolContext
from src.intent_analyzer.tools.db_engine import (
    engine_constraint_note,
    filter_paths_by_db_type,
)


def read_files(root: str, relative_paths: list[str]) -> dict[str, str]:
    """Read contents of specified files relative to root directory."""
    results = {}
    root_abs = os.path.abspath(root)
    for rel_path in relative_paths:
        full_path = os.path.join(root_abs, rel_path)
        if os.path.isfile(full_path):
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    results[rel_path] = f.read()
            except Exception as e:
                results[rel_path] = f"ERROR reading file: {e}"
    return results


def _extract_files(file_selector_output: object) -> list[str]:
    if hasattr(file_selector_output, "files"):
        return list(file_selector_output.files)
    if isinstance(file_selector_output, str):
        try:
            import json
            parsed = json.loads(file_selector_output)
            if isinstance(parsed, dict):
                return parsed.get("files", [])
        except Exception:
            return []
    if isinstance(file_selector_output, dict):
        return file_selector_output.get("files", [])
    return []


def read_selected_files(tool_context: ToolContext) -> str:
    """Read the contents of the database-relevant files selected by the file selector.

    Reads ``target`` and ``file_selector_output`` from session state and returns
    the file contents formatted for analysis.
    """
    target = tool_context.state.get("target", "")
    db_type = tool_context.state.get("db_type", "")
    file_selector_output = tool_context.state.get("file_selector_output")
    if not target or not file_selector_output:
        return "ERROR: target or file_selector_output not set in state"
    selected = _extract_files(file_selector_output)
    if not selected:
        return "ERROR: file_selector_output has no files"
    if db_type:
        selected = filter_paths_by_db_type(selected, db_type)
        if not selected:
            return "ERROR: no files could be read"
    contents = read_files(target, list(selected))
    if not contents:
        return "ERROR: no files could be read"
    parts = [f"=== {path} ===\n{content[:128000]}" for path, content in contents.items()]
    result = "\n\n".join(parts)
    if db_type:
        result = engine_constraint_note(db_type) + "\n\n" + result
    return result