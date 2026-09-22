"""Generic static analysis of SQL strings embedded in Python source.

The analyzer is intentionally conservative and repo-agnostic. It resolves SQL
string expressions on a best-effort basis and reports only issues that can be
determined with confidence:

* row index reads that exceed the number of columns a resolved ``SELECT`` returns
* ``SELECT`` statements whose ``FROM`` clause comma-cross-joins a derived table

Public API returns plain dicts so the module is importable anywhere.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Optional

_SENTINEL = "\u0000"
_FORMAT_SPEC_RE = re.compile(r"%(?:\d+)?[sdfr]")
_QUALIFIED_STAR_RE = re.compile(r'^[\w"`]+(?:\.[\w"`]+)*\.\*$')
_CROSS_JOIN_DERIVED_RE = re.compile(r"\bFROM\b[\s\S]*?,\s*\(\s*SELECT\b", re.IGNORECASE)
_FETCH_METHODS = {"fetchall", "fetchone", "fetchmany"}
_EXECUTE_METHODS = {"execute", "executemany"}
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _const_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _const_int(node: ast.AST) -> Optional[int]:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    return None


def _is_call_attr(node: ast.AST, names: set) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in names
    )


def _is_fetch_call(node: ast.AST) -> bool:
    return _is_call_attr(node, _FETCH_METHODS)


def _is_execute_call(node: ast.AST) -> bool:
    return _is_call_attr(node, _EXECUTE_METHODS)


def _literal_dict(node: ast.AST) -> Optional[Dict[str, Any]]:
    if not isinstance(node, ast.Dict):
        return None
    out: Dict[str, Any] = {}
    for key, value in zip(node.keys, node.values):
        k = _const_str(key)
        if k is None:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            out[k] = value.value
        elif isinstance(value, ast.Dict):
            sub = _literal_dict(value)
            if sub is not None:
                out[k] = sub
    return out


def _collect_module_dicts(tree: ast.Module) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    d = _literal_dict(node.value)
                    if d is not None:
                        result[target.id] = d
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            d = _literal_dict(node.value)
            if d is not None:
                result[node.target.id] = d
    return result


def _eval(node: Optional[ast.AST], local_vars: Dict[str, Any], module_dicts: Dict[str, Any]) -> Any:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        if node.id in local_vars:
            return local_vars[node.id]
        if node.id in module_dicts:
            return module_dicts[node.id]
        return None
    if isinstance(node, ast.Subscript):
        base = _eval(node.value, local_vars, module_dicts)
        if isinstance(base, dict):
            key = _const_str(node.slice)
            if key is None:
                return None
            return base.get(key)
        return None
    if isinstance(node, ast.BinOp):
        return _eval_binop(node, local_vars, module_dicts)
    if isinstance(node, ast.Call):
        return _eval_call(node, local_vars, module_dicts)
    if isinstance(node, ast.JoinedStr):
        return _eval_joined_str(node)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [_eval(elt, local_vars, module_dicts) for elt in node.elts]
    return None


def _eval_binop(node: ast.BinOp, local_vars: Dict[str, Any], module_dicts: Dict[str, Any]) -> Any:
    if isinstance(node.op, ast.Mod):
        fmt = _eval(node.left, local_vars, module_dicts)
        if not isinstance(fmt, str):
            return None
        count = len(_FORMAT_SPEC_RE.findall(fmt.replace("%%", "")))
        try:
            return fmt % ((0,) * count)
        except (TypeError, ValueError):
            return None
    if isinstance(node.op, ast.Add):
        left = _eval(node.left, local_vars, module_dicts)
        right = _eval(node.right, local_vars, module_dicts)
        if left is None and right is None:
            return None
        if left is not None and not isinstance(left, str):
            return None
        if right is not None and not isinstance(right, str):
            return None
        return (left if isinstance(left, str) else _SENTINEL) + (right if isinstance(right, str) else _SENTINEL)
    if isinstance(node.op, ast.Mult):
        left = _eval(node.left, local_vars, module_dicts)
        right = _eval(node.right, local_vars, module_dicts)
        li = _const_int(node.left)
        ri = _const_int(node.right)
        if isinstance(left, str) and ri is not None:
            return left * ri
        if isinstance(right, str) and li is not None:
            return right * li
        if isinstance(left, list) and ri is not None:
            return left * ri
        if isinstance(right, list) and li is not None:
            return right * li
        if isinstance(left, (str, list)) or isinstance(right, (str, list)):
            return _SENTINEL
        return None
    return None


def _eval_call(node: ast.Call, local_vars: Dict[str, Any], module_dicts: Dict[str, Any]) -> Any:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "replace" and len(node.args) >= 2:
        receiver = _eval(func.value, local_vars, module_dicts)
        needle = _eval(node.args[0], local_vars, module_dicts)
        replacement = _eval(node.args[1], local_vars, module_dicts)
        if not isinstance(receiver, str) or not isinstance(needle, str):
            return None
        repl = replacement if isinstance(replacement, str) else _SENTINEL
        try:
            return receiver.replace(needle, repl)
        except Exception:
            return None
    if isinstance(func, ast.Attribute) and func.attr == "join" and len(node.args) == 1:
        separator = _eval(func.value, local_vars, module_dicts)
        if not isinstance(separator, str):
            return None
        seq = node.args[0]
        if isinstance(seq, (ast.List, ast.Tuple, ast.Set)):
            parts: List[str] = []
            for elt in seq.elts:
                resolved = _eval(elt, local_vars, module_dicts)
                if not isinstance(resolved, str):
                    return _SENTINEL
                parts.append(resolved)
            return separator.join(parts)
        return _SENTINEL
    if isinstance(func, ast.Name) and func.id == "len":
        return _SENTINEL
    return None


def _eval_joined_str(node: ast.JoinedStr) -> str:
    parts: List[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            parts.append("0")
    return "".join(parts)


def _scan_keyword(sql: str, keyword: str, start: int, depth_target: int) -> int:
    kw = keyword.upper()
    depth = 0
    quote: Optional[str] = None
    i = start
    n = len(sql)
    while i < n:
        c = sql[i]
        if quote is not None:
            if c == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if c in ("'", '"', "`"):
            quote = c
            i += 1
            continue
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            depth -= 1
            i += 1
            continue
        if depth == depth_target and sql[i : i + len(kw)].upper() == kw:
            before = sql[i - 1] if i > 0 else " "
            after = sql[i + len(kw)] if i + len(kw) < n else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return i
        i += 1
    return -1


def _split_top_level(text: str) -> List[str]:
    items: List[str] = []
    current: List[str] = []
    depth = 0
    quote: Optional[str] = None
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if quote is not None:
            if c == quote:
                if i + 1 < n and text[i + 1] == quote:
                    current.append(c)
                    current.append(c)
                    i += 2
                    continue
                quote = None
            current.append(c)
        elif c in ("'", '"', "`"):
            quote = c
            current.append(c)
        elif c == "(":
            depth += 1
            current.append(c)
        elif c == ")":
            depth = max(0, depth - 1)
            current.append(c)
        elif c == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(c)
        i += 1
    items.append("".join(current))
    return items


def find_select_column_count(sql: str) -> Optional[int]:
    """Return the number of columns in the first top-level ``SELECT`` list.

    Returns ``None`` when the statement has ``SELECT *``, has no ``FROM``,
    cannot be parsed, or contains a select-list ``*``.
    """
    if not isinstance(sql, str) or not sql:
        return None
    select_pos = _scan_keyword(sql, "SELECT", 0, 0)
    if select_pos < 0:
        return None
    from_pos = _scan_keyword(sql, "FROM", select_pos + len("SELECT"), 0)
    if from_pos < 0:
        return None
    select_list = sql[select_pos + len("SELECT") : from_pos].strip()
    if not select_list:
        return None
    items = [item.strip() for item in _split_top_level(select_list)]
    items = [item for item in items if item]
    if not items:
        return None
    for item in items:
        normalized = re.sub(r"^(DISTINCT|ALL)\s+", "", item, flags=re.IGNORECASE).strip()
        if normalized == "*" or _QUALIFIED_STAR_RE.match(normalized):
            return None
    return len(items)


class _FunctionAnalyzer:
    def __init__(self, function_name: str, module_dicts: Dict[str, Any]) -> None:
        self.function_name = function_name
        self.module_dicts = module_dicts
        self.local_vars: Dict[str, Any] = {}
        self.last_sql: Optional[str] = None
        self.row_bindings: Dict[str, str] = {}
        self.row_reads: Dict[str, List[tuple]] = {}
        self.sql_candidates: List[tuple] = []

    def eval(self, node: Optional[ast.AST]) -> Any:
        return _eval(node, self.local_vars, self.module_dicts)

    def analyze(self, body: List[ast.stmt]) -> None:
        self.process_body(body)

    def process_body(self, body: List[ast.stmt]) -> None:
        for stmt in body:
            self.process_stmt(stmt)

    def process_stmt(self, stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.Assign):
            self._process_assign(stmt)
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None:
                resolved = self.eval(stmt.value)
                if isinstance(resolved, str):
                    self._record_candidate(resolved, stmt.lineno)
                if isinstance(stmt.target, ast.Name):
                    if _is_fetch_call(stmt.value):
                        self.local_vars[stmt.target.id] = self.last_sql
                    else:
                        self.local_vars[stmt.target.id] = resolved
                self.collect_row_reads(stmt.value)
        elif isinstance(stmt, ast.AugAssign):
            self.collect_row_reads(stmt.value)
        elif isinstance(stmt, ast.Expr):
            call = stmt.value
            if _is_execute_call(call) and call.args:
                sql = self.eval(call.args[0])
                self.last_sql = sql if isinstance(sql, str) else None
                if isinstance(sql, str):
                    self._record_candidate(sql, stmt.lineno)
            self.collect_row_reads(stmt.value)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            self._process_for(stmt)
        elif isinstance(stmt, ast.While):
            self.collect_row_reads(stmt.test)
            self.process_body(stmt.body)
            self.process_body(stmt.orelse)
        elif isinstance(stmt, ast.If):
            self.collect_row_reads(stmt.test)
            self.process_body(stmt.body)
            self.process_body(stmt.orelse)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                self.collect_row_reads(item.context_expr)
            self.process_body(stmt.body)
        elif isinstance(stmt, ast.Try):
            self.process_body(stmt.body)
            for handler in stmt.handlers:
                self.process_body(handler.body)
            self.process_body(stmt.orelse)
            self.process_body(stmt.finalbody)
        elif isinstance(stmt, _FUNCTION_NODES):
            return
        else:
            self.collect_row_reads(stmt)

    def _process_assign(self, stmt: ast.Assign) -> None:
        value = stmt.value
        resolved = self.eval(value)
        if isinstance(resolved, str):
            self._record_candidate(resolved, stmt.lineno)
        is_fetch = _is_fetch_call(value)
        for target in stmt.targets:
            if isinstance(target, ast.Name):
                self.local_vars[target.id] = self.last_sql if is_fetch else resolved
        self.collect_row_reads(value)
        self._process_row_unpack(stmt)

    def _process_for(self, stmt: ast.AST) -> None:
        sql = self._iter_sql(stmt.iter)
        if sql is None and isinstance(stmt.iter, ast.Name) and stmt.iter.id in self.row_bindings:
            sql = self.row_bindings[stmt.iter.id]
        saved = dict(self.row_bindings)
        if isinstance(sql, str):
            if isinstance(stmt.target, ast.Name):
                self.row_bindings[stmt.target.id] = sql
            elif isinstance(stmt.target, (ast.Tuple, ast.List)):
                self._add_indices(sql, list(range(len(stmt.target.elts))), stmt.lineno)
        self.process_body(stmt.body)
        self.row_bindings = saved
        self.process_body(stmt.orelse)

    def _iter_sql(self, node: ast.AST) -> Optional[str]:
        if _is_fetch_call(node):
            return self.last_sql
        if isinstance(node, ast.Name):
            value = self.local_vars.get(node.id)
            if isinstance(value, str):
                return value
        return None

    def collect_row_reads(self, node: Optional[ast.AST]) -> None:
        if node is None:
            return
        if isinstance(node, _FUNCTION_NODES):
            return
        if isinstance(node, (ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            self._process_comprehension(node)
            return
        if isinstance(node, ast.Assign):
            self._process_row_unpack(node)
        if isinstance(node, ast.Subscript):
            self._process_subscript(node)
        for child in ast.iter_child_nodes(node):
            self.collect_row_reads(child)

    def _process_comprehension(self, comp: ast.AST) -> None:
        generators = comp.generators
        if len(generators) != 1:
            return
        generator = generators[0]
        sql = self._iter_sql(generator.iter)
        if not isinstance(sql, str):
            return
        saved = dict(self.row_bindings)
        if isinstance(generator.target, ast.Name):
            self.row_bindings[generator.target.id] = sql
            exprs: List[Optional[ast.AST]] = (
                [comp.key, comp.value] if isinstance(comp, ast.DictComp) else [comp.elt]
            )
            exprs.extend(generator.ifs)
            for expr in exprs:
                self._collect_subscripts(expr, sql)
        elif isinstance(generator.target, (ast.Tuple, ast.List)):
            self._add_indices(sql, list(range(len(generator.target.elts))), comp.lineno)
        self.row_bindings = saved

    def _collect_subscripts(self, node: Optional[ast.AST], sql: str) -> None:
        if node is None:
            return
        if isinstance(node, _FUNCTION_NODES):
            return
        if isinstance(node, ast.Subscript):
            base = node.value
            if isinstance(base, ast.Name) and self.row_bindings.get(base.id) == sql:
                index = _const_int(node.slice)
                if index is not None and index >= 0:
                    self._add_index(sql, index, node.lineno)
        for child in ast.iter_child_nodes(node):
            self._collect_subscripts(child, sql)

    def _process_subscript(self, node: ast.Subscript) -> None:
        index = _const_int(node.slice)
        if index is None or index < 0:
            return
        base = node.value
        if isinstance(base, ast.Name):
            if base.id in self.row_bindings:
                self._add_index(self.row_bindings[base.id], index, node.lineno)
            elif isinstance(self.local_vars.get(base.id), str):
                self._add_index(self.local_vars[base.id], index, node.lineno)
        elif _is_fetch_call(base) and isinstance(self.last_sql, str):
            self._add_index(self.last_sql, index, node.lineno)

    def _process_row_unpack(self, stmt: ast.Assign) -> None:
        value = stmt.value
        if isinstance(value, ast.Name) and value.id in self.row_bindings:
            sql = self.row_bindings[value.id]
            for target in stmt.targets:
                if isinstance(target, (ast.Tuple, ast.List)):
                    self._add_indices(sql, list(range(len(target.elts))), stmt.lineno)

    def _add_index(self, sql: str, index: int, line: int) -> None:
        self.row_reads.setdefault(sql, []).append((index, line))

    def _add_indices(self, sql: str, indices: List[int], line: int) -> None:
        for index in indices:
            self._add_index(sql, index, line)

    def _record_candidate(self, sql: str, line: int) -> None:
        self.sql_candidates.append((sql, line))


def _iter_functions(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f"{node.name}.{child.name}", child


def row_index_violations(source: str) -> List[dict]:
    """Return row index reads that exceed the resolved SELECT column count."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    module_dicts = _collect_module_dicts(tree)
    violations: List[dict] = []
    seen = set()
    for name, node in _iter_functions(tree):
        analyzer = _FunctionAnalyzer(name, module_dicts)
        analyzer.analyze(node.body)
        for sql, reads in analyzer.row_reads.items():
            if not reads:
                continue
            column_count = find_select_column_count(sql)
            if column_count is None:
                continue
            max_index, line = max(reads, key=lambda item: item[0])
            if max_index < column_count:
                continue
            key = (name, sql, max_index, line)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {
                    "function": name,
                    "sql": sql,
                    "column_count": column_count,
                    "max_index": max_index,
                    "line": line,
                }
            )
    return violations


def planner_unfriendly_sql(source: str) -> List[dict]:
    """Return resolved SQL strings that comma-cross-join a derived table in FROM."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    module_dicts = _collect_module_dicts(tree)
    violations: List[dict] = []
    seen = set()
    for name, node in _iter_functions(tree):
        analyzer = _FunctionAnalyzer(name, module_dicts)
        analyzer.analyze(node.body)
        for sql, line in analyzer.sql_candidates:
            if not _CROSS_JOIN_DERIVED_RE.search(sql):
                continue
            key = (name, sql)
            if key in seen:
                continue
            seen.add(key)
            violations.append({"function": name, "sql": sql, "line": line})
    return violations
