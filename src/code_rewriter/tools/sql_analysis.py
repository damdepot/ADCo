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

from .sql_resolver import SENTINEL, collect_module_dicts, resolve
from .sql_resolver import _const_int, _const_str

_QUALIFIED_STAR_RE = re.compile(r'^[\w"`]+(?:\.[\w"`]+)*\.\*$')
_CROSS_JOIN_DERIVED_RE = re.compile(r"\bFROM\b[\s\S]*?,\s*\(\s*SELECT\b", re.IGNORECASE)
_FETCH_METHODS = {"fetchall", "fetchone", "fetchmany"}
_EXECUTE_METHODS = {"execute", "executemany"}
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


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


def _top_level_segments(sql: str) -> List[str]:
    """Split *sql* on semicolons at paren-depth 0, ignoring quotes."""
    segments: List[str] = []
    current: List[str] = []
    depth = 0
    quote: Optional[str] = None
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if quote is not None:
            if c == quote:
                if i + 1 < n and sql[i + 1] == quote:
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
        elif c == ";" and depth == 0:
            segments.append("".join(current))
            current = []
        else:
            current.append(c)
        i += 1
    segments.append("".join(current))
    return segments


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
        self.execute_sqls: List[tuple] = []
        self.unknown_query_keys: List[tuple] = []

    def eval(self, node: Optional[ast.AST]) -> Any:
        return resolve(node, self.local_vars, self.module_dicts)

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
                arg0 = call.args[0]
                if isinstance(arg0, ast.Subscript):
                    base = self.eval(arg0.value)
                    key = _const_str(arg0.slice)
                    if isinstance(base, dict) and key is not None and key not in base:
                        self.unknown_query_keys.append((key, stmt.lineno))
                sql = self.eval(call.args[0])
                self.last_sql = sql if isinstance(sql, str) else None
                if isinstance(sql, str):
                    self._record_candidate(sql, stmt.lineno)
                    self.execute_sqls.append((sql, stmt.lineno))
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
    module_dicts = collect_module_dicts(tree)
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
    module_dicts = collect_module_dicts(tree)
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


def multi_statement_sql(source: str) -> List[dict]:
    """Resolved execute() SQL strings containing >1 top-level statement."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    module_dicts = collect_module_dicts(tree)
    violations: List[dict] = []
    seen = set()
    for name, node in _iter_functions(tree):
        analyzer = _FunctionAnalyzer(name, module_dicts)
        analyzer.analyze(node.body)
        for sql, line in analyzer.execute_sqls:
            statements = sum(1 for seg in _top_level_segments(sql) if seg.strip())
            if statements < 2:
                continue
            key = (name, sql)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {"function": name, "sql": sql, "line": line, "statements": statements}
            )
    return violations


def unknown_query_key_sql(source: str) -> List[dict]:
    """Resolved query-dict keys referenced by execute() that do not exist."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    module_dicts = collect_module_dicts(tree)
    violations: List[dict] = []
    seen = set()
    for name, node in _iter_functions(tree):
        analyzer = _FunctionAnalyzer(name, module_dicts)
        analyzer.analyze(node.body)
        for key, line in analyzer.unknown_query_keys:
            dedup_key = (name, key, line)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            violations.append({"function": name, "key": key, "line": line})
    return violations


def duplicate_where_sql(source: str) -> List[dict]:
    """Resolved SQL strings with 2+ top-level WHERE clauses."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    module_dicts = collect_module_dicts(tree)
    violations: List[dict] = []
    seen = set()
    for name, node in _iter_functions(tree):
        analyzer = _FunctionAnalyzer(name, module_dicts)
        analyzer.analyze(node.body)
        for sql, line in analyzer.execute_sqls:
            if SENTINEL in sql:
                continue
            count = 0
            pos = 0
            while True:
                found = _scan_keyword(sql, "WHERE", pos, 0)
                if found < 0:
                    break
                count += 1
                pos = found + len("WHERE")
            if count < 2:
                continue
            key = (name, sql)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {"function": name, "sql": sql, "line": line, "where_count": count}
            )
    return violations
