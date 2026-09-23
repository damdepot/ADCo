"""Generic static analysis of Python-level correctness/performance defects.

The analyzer is intentionally conservative and repo-agnostic. It is stdlib-only
and its public API returns plain dicts so the module is importable anywhere.

* ``%`` formatting applied to a template whose conversion count does not match
  the number of supplied arguments (root cause of ``TypeError``)
* names loaded inside a function that are never bound in that function, any
  enclosing scope, the module, or builtins (root cause of ``NameError``)
* bulk-write ``cursor.executemany`` calls in a psycopg2 file that never uses the
  ``execute_batch``/``execute_values`` fast path
"""

from __future__ import annotations

import ast
import builtins
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .sql_analysis import _FUNCTION_NODES, _FunctionAnalyzer, _iter_functions
from .sql_resolver import SENTINEL, collect_module_dicts, resolve

_FORMAT_RE = re.compile(r"%(?:\(([A-Za-z_]\w*)\))?[-#0 +]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]")
_DYNAMIC_SCOPE_CALLS = {"globals", "locals", "eval", "exec"}
_FAST_PATH_NAMES = {"execute_batch", "execute_values"}
_BULK_PREFIXES = ("INSERT", "UPDATE", "DELETE")
_ALLOWED_DUNDERS = {
    "__file__",
    "__name__",
    "__doc__",
    "__package__",
    "__loader__",
    "__spec__",
    "__builtins__",
    "__class__",
}


def _scan_conversions(text: str) -> Tuple[int, Set[str]]:
    """Return ``(positional_count, named_names)`` for a ``%`` format template."""
    positional = 0
    named: Set[str] = set()
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "%":
            i += 1
            continue
        if i + 1 < n and text[i + 1] == "%":
            i += 2
            continue
        match = _FORMAT_RE.match(text, i)
        if match is None:
            i += 1
            continue
        name = match.group(1)
        if name is None:
            positional += 1
        else:
            named.add(name)
        i = match.end()
    return positional, named


def _raw_string(
    node: Optional[ast.AST],
    local_vars: Dict[str, Any],
    module_dicts: Dict[str, Any],
) -> Optional[str]:
    """Best-effort reconstruction of a format template, preserving ``%`` text."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                inner = _raw_string(value.value, local_vars, module_dicts)
                parts.append(inner if inner is not None else SENTINEL)
            else:
                parts.append(SENTINEL)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _raw_string(node.left, local_vars, module_dicts)
        right = _raw_string(node.right, local_vars, module_dicts)
        if left is None or right is None:
            return None
        return left + right
    resolved = resolve(node, local_vars, module_dicts)
    if isinstance(resolved, str):
        return resolved
    return None


def _supplied_args(
    node: Optional[ast.AST],
    local_vars: Dict[str, Any],
    module_dicts: Dict[str, Any],
) -> Optional[Tuple[int, int]]:
    """Return ``(positional, named)`` argument counts, or ``None`` if unknown."""
    if node is None:
        return None
    if isinstance(node, (ast.Tuple, ast.List)):
        if any(isinstance(elt, ast.Starred) for elt in node.elts):
            return None
        return (len(node.elts), 0)
    if isinstance(node, ast.Dict):
        return (0, sum(1 for key in node.keys if key is not None))
    resolved = resolve(node, local_vars, module_dicts)
    if isinstance(resolved, str):
        return (1, 0)
    if isinstance(resolved, list):
        return (len(resolved), 0)
    if isinstance(resolved, dict):
        return (0, len(resolved))
    return None


class _ModFormatAnalyzer(_FunctionAnalyzer):
    """Reuse the ordered scope tracking, recording ``BinOp(Mod)`` nodes."""

    def __init__(self, function_name: str, module_dicts: Dict[str, Any]) -> None:
        super().__init__(function_name, module_dicts)
        self.mod_nodes: List[Tuple[ast.BinOp, Dict[str, Any]]] = []

    def collect_row_reads(self, node: Optional[ast.AST]) -> None:
        if node is None:
            return
        if isinstance(node, _FUNCTION_NODES):
            return
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            self.mod_nodes.append((node, dict(self.local_vars)))
        for child in ast.iter_child_nodes(node):
            self.collect_row_reads(child)


def _record_percent_violation(
    violations: List[dict],
    seen: set,
    function: str,
    line: int,
    template: str,
    expected: int,
    actual: int,
) -> None:
    key = (function, template, line)
    if key in seen:
        return
    seen.add(key)
    violations.append(
        {
            "function": function,
            "line": line,
            "template": template,
            "expected": expected,
            "actual": actual,
        }
    )


def percent_format_arity_violations(source: str) -> List[dict]:
    """Return ``%`` format expressions whose conversion count mismatches args.

    Each violation is ``{"function", "line", "template", "expected", "actual"}``.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    module_dicts = collect_module_dicts(tree)
    violations: List[dict] = []
    seen = set()
    for name, node in _iter_functions(tree):
        analyzer = _ModFormatAnalyzer(name, module_dicts)
        analyzer.analyze(node.body)
        for binop, local_vars in analyzer.mod_nodes:
            template = _raw_string(binop.left, local_vars, module_dicts)
            if not isinstance(template, str):
                continue
            positional, named = _scan_conversions(template)
            # An f-string that carries a `%` conversion is already a collision:
            # the f-string interpolates, then `%` consumes both the conversion
            # and any SQL `%s` placeholders. Flag regardless of what the
            # interpolated fragments resolve to (they are often dynamic).
            if isinstance(binop.left, ast.JoinedStr) and (positional or named):
                if binop.right is not None:
                    supplied = _supplied_args(binop.right, local_vars, module_dicts) or (0, 0)
                    _record_percent_violation(
                        violations,
                        seen,
                        name,
                        binop.lineno,
                        template,
                        positional + len(named),
                        supplied[0] + supplied[1],
                    )
                continue
            if SENTINEL in template:
                continue
            supplied = _supplied_args(binop.right, local_vars, module_dicts)
            if supplied is None:
                continue
            if named:
                expected = len(named)
                actual = supplied[1]
            else:
                expected = positional
                actual = supplied[0]
            if expected == actual:
                continue
            _record_percent_violation(
                violations, seen, name, binop.lineno, template, expected, actual
            )
    return violations


def _target_names(node: Optional[ast.AST]) -> Set[str]:
    if node is None:
        return set()
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        names: Set[str] = set()
        for elt in node.elts:
            names |= _target_names(elt)
        return names
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    return set()


def _bound_names(node: ast.AST) -> Set[str]:
    """Names bound by *node* at module scope (does not descend into scopes)."""
    names: Set[str] = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            names |= _target_names(target)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        names |= _target_names(node.target)
    elif isinstance(node, ast.Import):
        for alias in node.names:
            names.add(alias.asname or alias.name.split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name != "*":
                names.add(alias.asname or alias.name)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        names |= _target_names(node.target)
        for child in list(node.body) + list(node.orelse):
            names |= _bound_names(child)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            names |= _target_names(item.optional_vars)
        for child in node.body:
            names |= _bound_names(child)
    elif isinstance(node, ast.Try):
        for child in list(node.body) + list(node.orelse) + list(node.finalbody):
            names |= _bound_names(child)
        for handler in node.handlers:
            if handler.name:
                names.add(handler.name)
            for child in handler.body:
                names |= _bound_names(child)
    elif isinstance(node, (ast.If, ast.While)):
        for child in list(node.body) + list(node.orelse):
            names |= _bound_names(child)
    return names


def _collect(
    node: Optional[ast.AST],
    bound: Set[str],
    referenced: Set[str],
    ref_lines: Dict[str, int],
) -> None:
    """Collect bound/referenced names without descending into nested scopes."""
    if node is None:
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        bound.add(node.name)
        return
    if isinstance(node, ast.Lambda):
        return
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, ast.Load):
            referenced.add(node.id)
            ref_lines.setdefault(node.id, node.lineno)
        else:
            bound.add(node.id)
        return
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        bound.update(node.names)
        return
    if isinstance(node, ast.Assign):
        for target in node.targets:
            _collect(target, bound, referenced, ref_lines)
        _collect(node.value, bound, referenced, ref_lines)
        return
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        _collect(node.target, bound, referenced, ref_lines)
        _collect(node.value, bound, referenced, ref_lines)
        return
    if isinstance(node, (ast.For, ast.AsyncFor)):
        _collect(node.target, bound, referenced, ref_lines)
        _collect(node.iter, bound, referenced, ref_lines)
        for child in list(node.body) + list(node.orelse):
            _collect(child, bound, referenced, ref_lines)
        return
    if isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            _collect(item.context_expr, bound, referenced, ref_lines)
            _collect(item.optional_vars, bound, referenced, ref_lines)
        for child in node.body:
            _collect(child, bound, referenced, ref_lines)
        return
    if isinstance(node, ast.Try):
        for child in list(node.body) + list(node.orelse) + list(node.finalbody):
            _collect(child, bound, referenced, ref_lines)
        for handler in node.handlers:
            _collect(handler.type, bound, referenced, ref_lines)
            if handler.name:
                bound.add(handler.name)
            for child in handler.body:
                _collect(child, bound, referenced, ref_lines)
        return
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        for generator in node.generators:
            _collect(generator.iter, bound, referenced, ref_lines)
            _collect(generator.target, bound, referenced, ref_lines)
            for condition in generator.ifs:
                _collect(condition, bound, referenced, ref_lines)
        if isinstance(node, ast.DictComp):
            _collect(node.key, bound, referenced, ref_lines)
            _collect(node.value, bound, referenced, ref_lines)
        else:
            _collect(node.elt, bound, referenced, ref_lines)
        return
    if isinstance(node, ast.NamedExpr):
        _collect(node.target, bound, referenced, ref_lines)
        _collect(node.value, bound, referenced, ref_lines)
        return
    if isinstance(node, ast.Import):
        for alias in node.names:
            bound.add(alias.asname or alias.name.split(".")[0])
        return
    if isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name != "*":
                bound.add(alias.asname or alias.name)
        return
    for child in ast.iter_child_nodes(node):
        _collect(child, bound, referenced, ref_lines)


def _has_star_import(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                return True
    return False


def _is_dynamic_scope(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Match):
            return True
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id in _DYNAMIC_SCOPE_CALLS
        ):
            return True
    return False


def undefined_name_violations(source: str) -> List[dict]:
    """Return names loaded inside a function but never bound in any scope.

    Each violation is ``{"function", "line", "name"}``.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    if _has_star_import(tree):
        return []
    module_bound: Set[str] = set()
    for node in tree.body:
        module_bound |= _bound_names(node)
    allowed = module_bound | set(dir(builtins)) | _ALLOWED_DUNDERS
    violations: List[dict] = []
    seen = set()
    for name, node in _iter_functions(tree):
        if _is_dynamic_scope(node):
            continue
        bound: Set[str] = set()
        referenced: Set[str] = set()
        ref_lines: Dict[str, int] = {}
        args = node.args
        for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            bound.add(arg.arg)
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)
        for stmt in node.body:
            _collect(stmt, bound, referenced, ref_lines)
        for undefined in sorted(referenced - bound - allowed):
            key = (name, undefined)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {
                    "function": name,
                    "line": ref_lines.get(undefined),
                    "name": undefined,
                }
            )
    return violations


def _imports_psycopg2(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "psycopg2" or alias.name.startswith("psycopg2."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "psycopg2" or module.startswith("psycopg2."):
                return True
    return False


def _references_fast_path(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FAST_PATH_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _FAST_PATH_NAMES:
            return True
        if isinstance(node, ast.alias) and node.name in _FAST_PATH_NAMES:
            return True
    return False


def _walk_scoped(node: ast.AST):
    if isinstance(node, _FUNCTION_NODES):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _walk_scoped(child)


def slow_executemany_violations(source: str) -> List[dict]:
    """Return bulk-write ``executemany`` calls in a psycopg2 file.

    Each violation is ``{"function", "line"}``.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    if not _imports_psycopg2(tree):
        return []
    if _references_fast_path(tree):
        return []
    module_dicts = collect_module_dicts(tree)
    violations: List[dict] = []
    seen = set()
    for name, node in _iter_functions(tree):
        analyzer = _FunctionAnalyzer(name, module_dicts)
        analyzer.analyze(node.body)
        for stmt in node.body:
            for child in _walk_scoped(stmt):
                if not isinstance(child, ast.Call):
                    continue
                func = child.func
                if not (isinstance(func, ast.Attribute) and func.attr == "executemany"):
                    continue
                if not child.args:
                    continue
                sql = resolve(child.args[0], analyzer.local_vars, module_dicts)
                if not isinstance(sql, str):
                    continue
                if not sql.strip().upper().startswith(_BULK_PREFIXES):
                    continue
                key = (name, child.lineno)
                if key in seen:
                    continue
                seen.add(key)
                violations.append({"function": name, "line": child.lineno})
    return violations
