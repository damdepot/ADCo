"""Helpers for SINGLE_QUERY contracts: one SQL statement behind a function.

A SINGLE_QUERY target is a function that issues exactly one SQL statement whose
text lives in a plain-string ``QUERY_<n>`` class constant. That constant is the
only form the stats pydecompiler extracts, so rewriting such a target means
editing the constant, not the function body.
"""

from __future__ import annotations

import ast
import re
from typing import Optional

from .._common import extract_function_source_by_name

SINGLE_QUERY_PATTERN = "SINGLE_QUERY"

_QUERY_CONST_NAME_RE = re.compile(r"^QUERY_\d+$")
_EXECUTE_METHODS = ("execute", "executemany")


def _parse(source: str) -> Optional[ast.Module]:
    if not source:
        return None
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def _class_named(
    tree: ast.Module, class_name: Optional[str]
) -> Optional[ast.ClassDef]:
    if not class_name:
        return None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _iter_assigns(owner: ast.AST, const_name: str):
    for child in getattr(owner, "body", []):
        if isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name) and target.id == const_name:
                    yield child
                    break


def owning_class_name(source: str, qualified_function: str) -> Optional[str]:
    """Return the class owning *qualified_function*, or None if module-level.

    Accepts a qualified name (``Class.method``) or a bare method name resolved by
    scanning classes. Returns None when the function is module-level or unknown.
    """
    tree = _parse(source)
    if tree is None or not qualified_function:
        return None
    bare = qualified_function.rsplit(".", 1)[-1]

    def _has_method(cls: ast.ClassDef) -> bool:
        return any(
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name == bare
            for child in cls.body
        )

    if "." in qualified_function:
        prefix = qualified_function.rsplit(".", 1)[0]
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == prefix and _has_method(node):
                return prefix
        return None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and _has_method(node):
            return node.name
    return None


def referenced_query_constant(function_source: str) -> Optional[str]:
    """Return the ``QUERY_<n>`` constant a function passes to execute().

    Matches ``cur.execute(self.QUERY_<n>)`` (and ``executemany``); returns the
    constant name, or None when no such reference exists.
    """
    tree = _parse(function_source)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in _EXECUTE_METHODS or not node.args:
            continue
        arg = node.args[0]
        if (
            isinstance(arg, ast.Attribute)
            and isinstance(arg.value, ast.Name)
            and arg.value.id == "self"
            and _QUERY_CONST_NAME_RE.match(arg.attr)
        ):
            return arg.attr
    return None


def get_class_constant(
    source: str, class_name: Optional[str], const_name: str
) -> tuple[bool, Optional[str]]:
    """Return ``(exists, value)`` for a class/module constant.

    ``value`` is the literal text when the constant is assigned a plain string
    (``ast.Constant`` str); it is None when the name exists but is not a plain
    string. ``exists`` is False when the constant is not found.
    """
    tree = _parse(source)
    if tree is None:
        return False, None
    owner = _class_named(tree, class_name) if class_name else tree
    if owner is None:
        return False, None
    for assign in _iter_assigns(owner, const_name):
        value_node = assign.value
        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            return True, value_node.value
        return True, None
    return False, None


def constant_ast_dump(
    source: str, class_name: Optional[str], const_name: str
) -> Optional[str]:
    """Return a normalized AST dump of a constant's value node, or None."""
    tree = _parse(source)
    if tree is None:
        return None
    owner = _class_named(tree, class_name) if class_name else tree
    if owner is None:
        return None
    for assign in _iter_assigns(owner, const_name):
        return ast.dump(assign.value)
    return None


def target_constant_name(source: str, qualified_function: str) -> Optional[str]:
    """Return the QUERY_<n> constant referenced by a target function, if any."""
    fn_source = extract_function_source_by_name(source, qualified_function)
    return referenced_query_constant(fn_source)


def target_constant(
    source: str, qualified_function: str
) -> tuple[Optional[str], bool, Optional[str]]:
    """Return ``(const_name, exists, value)`` for a target's QUERY_<n> constant.

    ``const_name`` is None when the function references no QUERY_<n> constant.
    """
    name = target_constant_name(source, qualified_function)
    if name is None:
        return None, False, None
    cls = owning_class_name(source, qualified_function)
    exists, value = get_class_constant(source, cls, name)
    return name, exists, value


def _render_string_literal(value: str) -> str:
    if '"""' not in value:
        return f'"""{value}"""'
    return repr(value)


def replace_class_constant(
    source: str, class_name: Optional[str], const_name: str, new_value: str
) -> tuple[bool, str, Optional[str]]:
    """Replace a class/module string constant's value with *new_value*.

    Returns ``(success, reconstructed_source, error_message)``. Mirrors
    ``replace_function_ast``: line-slice the assignment, then ``compile``-validate
    the whole module.
    """
    tree = _parse(source)
    if tree is None:
        return False, source, "Invalid source code syntax"
    owner = _class_named(tree, class_name) if class_name else tree
    if owner is None:
        return False, source, f"Class '{class_name}' not found"

    target: Optional[ast.Assign] = None
    for assign in _iter_assigns(owner, const_name):
        target = assign
        break
    if target is None:
        return False, source, f"Constant '{const_name}' not found"

    indent = " " * target.col_offset
    new_lines = [f"{indent}{const_name} = {_render_string_literal(new_value)}"]
    start = target.lineno
    end = getattr(target, "end_lineno", target.lineno)
    source_lines = source.splitlines()
    reconstructed = "\n".join(
        source_lines[: start - 1] + new_lines + source_lines[end:]
    )
    if source.endswith("\n"):
        reconstructed += "\n"

    try:
        compile(reconstructed, "<string>", "exec")
    except Exception as e:  # noqa: BLE001
        return False, source, f"Invalid replacement code syntax: {e}"
    return True, reconstructed, None
