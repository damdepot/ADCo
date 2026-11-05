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
import symtable
from typing import Any, Dict, List, Optional, Set, Tuple

from .sql_analysis import _FUNCTION_NODES, _FunctionAnalyzer, _iter_analyzed, _iter_functions
from .sql_resolver import SENTINEL, collect_module_dicts, resolve
from .._common import target_names

_FORMAT_RE = re.compile(r"%(?:\(([A-Za-z_]\w*)\))?[-#0 +]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]")
_DYNAMIC_SCOPE_CALLS = {"globals", "locals", "eval", "exec"}
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
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source, _ModFormatAnalyzer):
        for binop, local_vars in analyzer.mod_nodes:
            template = _raw_string(binop.left, local_vars, analyzer.module_dicts)
            if not isinstance(template, str):
                continue
            positional, named = _scan_conversions(template)
            # An f-string that carries a `%` conversion is already a collision:
            # the f-string interpolates, then `%` consumes both the conversion
            # and any SQL `%s` placeholders. Flag regardless of what the
            # interpolated fragments resolve to (they are often dynamic).
            if isinstance(binop.left, ast.JoinedStr) and (positional or named):
                if binop.right is not None:
                    supplied = _supplied_args(binop.right, local_vars, analyzer.module_dicts) or (0, 0)
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
            supplied = _supplied_args(binop.right, local_vars, analyzer.module_dicts)
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


def _bound_names(node: ast.AST) -> Set[str]:
    """Names bound by *node* at module scope (does not descend into scopes)."""
    names: Set[str] = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            names |= set(target_names(target))
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        names |= set(target_names(node.target))
    elif isinstance(node, ast.Import):
        for alias in node.names:
            names.add(alias.asname or alias.name.split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name != "*":
                names.add(alias.asname or alias.name)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        names |= set(target_names(node.target))
        for child in list(node.body) + list(node.orelse):
            names |= _bound_names(child)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            names |= set(target_names(item.optional_vars))
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


def _has_star_import(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                return True
    return False


def _is_dynamic_scope(node: ast.AST) -> bool:
    """Return True when *node*'s name bindings cannot be resolved statically.

    ``globals``/``locals``/``eval``/``exec`` invent bindings the symtable cannot
    see, so any scope that calls one is skipped. Match statements, class
    bodies, comprehensions and lambdas are all modelled by ``symtable`` and are
    therefore analysed normally.
    """
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id in _DYNAMIC_SCOPE_CALLS
        ):
            return True
    return False


def _qualify(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _index_scope_nodes(
    node: ast.AST,
    prefix: str,
    index: Dict[Tuple[str, int, str], Tuple[ast.AST, str]],
) -> None:
    """Index defining AST nodes by the key ``symtable`` uses to describe them."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        qualified = _qualify(prefix, node.name)
        index[("def", node.lineno, node.name)] = (node, qualified)
        for stmt in node.body:
            _index_scope_nodes(stmt, qualified, index)
        return
    if isinstance(node, ast.ClassDef):
        qualified = _qualify(prefix, node.name)
        for stmt in node.body:
            _index_scope_nodes(stmt, qualified, index)
        return
    if isinstance(node, ast.Lambda):
        qualified = _qualify(prefix, "<lambda>")
        index[("lambda", node.lineno, "lambda")] = (node, qualified)
        for child in ast.iter_child_nodes(node.body):
            _index_scope_nodes(child, qualified, index)
        return
    for child in ast.iter_child_nodes(node):
        _index_scope_nodes(child, prefix, index)


def _walk_local(node: ast.AST):
    """Yield descendants of *node* without entering nested scopes."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _FUNCTION_NODES):
            continue
        yield child
        yield from _walk_local(child)


def _first_load_line(node: ast.AST, name: str) -> Optional[int]:
    """Return the first line inside *node* that loads *name*, if any."""
    lines = [
        child.lineno
        for child in _walk_local(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id == name
    ]
    return min(lines) if lines else None


def _module_scope_names(table: symtable.SymbolTable) -> Set[str]:
    """Return names the module itself binds (assigned, imported or parameter)."""
    return {
        symbol.get_name()
        for symbol in table.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_parameter()
    }


def undefined_name_violations(
    source: str, allowed_extra: Set[str] | None = None
) -> List[dict]:
    """Return names loaded inside a function but never bound in any scope.

    Each violation is ``{"function", "line", "name"}``.

    Scope resolution comes from :mod:`symtable`, so comprehension, lambda,
    generator-expression and class-body bindings are modelled exactly as the
    compiler models them. A name is reported when the compiler marks it as a
    global reference inside a function/lambda scope and nothing defines it at
    module scope or in builtins. Class bodies themselves are never reported
    (they execute in module scope) but their methods are.

    Two modes:

    * ``allowed_extra is None`` — a file-level star import disables the check
      entirely (``[]``), because the star module may bind any name and callers
      without the original source cannot tell.
    * ``allowed_extra`` is a set of names — the check still runs on a
      star-importing file and those names are unioned into the allowlist. A
      star import may legitimately supply module-level names, so the only
      sound signal without resolving the star module is "a NEW identifier
      that nothing defines and that the caller's reference set never
      mentions".
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    if allowed_extra is None and _has_star_import(tree):
        return []
    try:
        top = symtable.symtable(source, "<verify>", "exec")
    except SyntaxError:  # pragma: no cover - ast.parse already succeeded
        return []
    allowed = _module_scope_names(top) | set(dir(builtins)) | _ALLOWED_DUNDERS
    if allowed_extra:
        allowed |= allowed_extra
    index: Dict[Tuple[str, int, str], Tuple[ast.AST, str]] = {}
    _index_scope_nodes(tree, "", index)
    violations: List[dict] = []
    seen: Set[Tuple[str, str]] = set()

    def record(scope: symtable.SymbolTable, node: Optional[ast.AST], qualified: str) -> None:
        for symbol in scope.get_symbols():
            name = symbol.get_name()
            if not (symbol.is_referenced() and symbol.is_global()):
                continue
            if name in allowed:
                continue
            key = (qualified, name)
            if key in seen:
                continue
            seen.add(key)
            line = _first_load_line(node, name) if node is not None else None
            violations.append(
                {
                    "function": qualified,
                    "line": line if line is not None else scope.get_lineno(),
                    "name": name,
                }
            )

    def visit(table: symtable.SymbolTable) -> None:
        for child in table.get_children():
            scope_type = child.get_type()
            if scope_type == "class":
                # Class bodies run in module scope: never report from them, but
                # their methods are ordinary function scopes.
                visit(child)
                continue
            if scope_type != "function":
                visit(child)
                continue
            entry = index.get(("lambda" if child.get_name() == "lambda" else "def",
                                child.get_lineno(), child.get_name()))
            node = entry[0] if entry is not None else None
            if node is not None and _is_dynamic_scope(node):
                continue
            record(child, node, entry[1] if entry is not None else child.get_name())
            visit(child)

    visit(top)
    violations.sort(key=lambda violation: (violation["function"], violation["name"]))
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
    # No file-level fast-path exemption: a sibling function using execute_batch
    # must not hide new executemany sites elsewhere in the file.
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
