"""Shared helpers for the code_rewriter package."""

from __future__ import annotations

import ast
import asyncio
import json
import re


def _maybe_parse(value: object) -> dict:
    """Return *value* as a dict, JSON-parsing strings (stripping markdown fences)."""
    if isinstance(value, str):
        stripped = re.sub(r"^```[a-z]*\n?", "", value.strip(), flags=re.MULTILINE)
        stripped = re.sub(r"```$", "", stripped.strip())
        try:
            return json.loads(stripped.strip())
        except (json.JSONDecodeError, ValueError):
            return {}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)):
        return [_maybe_parse(item) for item in value]
    return {}


def make_buffer_callback(buffer_time: float = 0.0):
    if buffer_time <= 0:
        return None

    async def buffer_callback(callback_context=None, llm_request=None, **kwargs):
        await asyncio.sleep(buffer_time)
    return buffer_callback


def format_intent_lines(
    intent_output: dict,
    *,
    always_notes: bool = False,
    targets_header: str = "",
) -> str:
    """Build the intent text block shared by the planner and code optimizer."""
    lines = [
        f"CONNECTION: {intent_output.get('connection', '')}",
        f"QUERIES: {intent_output.get('queries', '')}",
        f"TRANSACTIONS: {intent_output.get('transactions', '')}",
        f"N_PLUS_ONE: {intent_output.get('n_plus_one', '')}",
        f"CONCURRENCY: {intent_output.get('concurrency', '')}",
        f"ORM: {intent_output.get('orm', '')}",
    ]
    notes = intent_output.get("notes", "")
    if notes or always_notes:
        lines.append(f"NOTES: {notes}")
    if targets_header:
        lines.append(targets_header)
    for t in intent_output.get("optimization_targets", []) or []:
        lines.append(f"- {t.get('file', '')}: {t.get('description', '')}")
    return "\n".join(lines)


def find_function_node(tree: ast.Module, qualified: str) -> ast.AST | None:
    """Return the AST node for a (qualified) function/method name, or ``None``.

    Walks classes and nested functions, matching either the fully qualified name
    (``Class.method`` / ``outer.inner``) or the bare function name.
    """
    if not qualified:
        return None
    bare = qualified.rsplit(".", 1)[-1]

    def _walk(node: ast.AST, prefix: str) -> ast.AST | None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                found = _walk(child, f"{prefix}{child.name}.")
                if found is not None:
                    return found
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                full = f"{prefix}{child.name}"
                if full == qualified or child.name == bare:
                    return child
                found = _walk(child, f"{full}.")
                if found is not None:
                    return found
            else:
                found = _walk(child, prefix)
                if found is not None:
                    return found
        return None

    return _walk(tree, "")


def target_names(node: ast.AST) -> list[str]:
    """Return the names bound by an assignment/for target node."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in node.elts:
            names.extend(target_names(elt))
        return names
    if isinstance(node, ast.Starred):
        return target_names(node.value)
    return []


def extract_function_source_by_name(source: str, qualified: str) -> str:
    """Extract the source of a function/method by (qualified) name.

    Matches either the fully qualified name (``Class.method`` / ``outer.inner``)
    or the bare function name. Returns "" if the source cannot be parsed or the
    function is not found.
    """
    if not source or not qualified:
        return ""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ""

    node = find_function_node(tree, qualified)
    if node is None:
        return ""
    return ast.get_source_segment(source, node) or ""
