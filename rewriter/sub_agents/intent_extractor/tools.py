"""Tools for the intent extractor agent — read selected files."""

from google.adk.tools import ToolContext

from rewriter.tools.copier import read_files


def read_selected_files(tool_context: ToolContext) -> str:
    """Read the contents of the database-relevant files selected by the file selector.

    Reads ``target`` and ``file_selector_output`` from session state and returns
    the file contents formatted for analysis.
    """
    target = tool_context.state.get("target", "")
    file_selector_output = tool_context.state.get("file_selector_output")
    if not target or not file_selector_output:
        return "ERROR: target or file_selector_output not set in state"
    selected = file_selector_output.get("files", [])
    if not selected:
        return "ERROR: file_selector_output has no files"
    contents = read_files(target, list(selected))
    if not contents:
        return "ERROR: no files could be read"
    parts = [f"=== {path} ===\n{content[:4000]}" for path, content in contents.items()]
    return "\n\n".join(parts)