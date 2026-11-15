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
from itertools import permutations
from typing import Any, Dict, List, Optional

from .sql_resolver import SENTINEL, collect_module_dicts, resolve
from .sql_resolver import _const_int, _const_str

_QUALIFIED_STAR_RE = re.compile(r'^[\w"`]+(?:\.[\w"`]+)*\.\*$')
_CROSS_JOIN_DERIVED_RE = re.compile(r"\bFROM\b[\s\S]*?,\s*\(\s*SELECT\b", re.IGNORECASE)
_FETCH_METHODS = {"fetchall", "fetchone", "fetchmany"}
_EXECUTE_METHODS = {"execute", "executemany"}
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_IMPLICIT_JOIN_MIN_RELATIONS = 3
_FROM_CLAUSE_END_KEYWORDS = (
    "WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "UNION", "EXCEPT", "INTERSECT",
    "WINDOW", "QUALIFY", "RETURNING", "FETCH", "FOR",
)
_PLACEHOLDER_NAMED_RE = re.compile(r"%\(([A-Za-z_]\w*)\)s")
_PLACEHOLDER_POSITIONAL_RE = re.compile(r"%s|\$\d+|(?<!:):[A-Za-z_]\w*|\?")
_PREDICATE_RE = re.compile(
    r'^\s*([A-Za-z_"`][\w."`]*)\s*(=\s*(?:ANY|ALL)\b|IN\b|<>|!=|>=|<=|=|>|<)',
    re.IGNORECASE,
)
_SELECT_AS_ALIAS_RE = re.compile(
    r'\s+AS\s+("[^"]*"|`[^`]*`|[A-Za-z_]\w*)\s*$', re.IGNORECASE
)
_SELECT_BARE_ALIAS_RE = re.compile(r'\s+("[^"]*"|`[^`]*`|[A-Za-z_]\w*)\s*$')
_FRAGILE_COMPOSITE_AGG_RE = re.compile(
    r"ARRAY_AGG\s*\(\s*(?:DISTINCT\s+)?ROW\s*\(", re.IGNORECASE
)
_COMPOSITE_ANY_ARRAY_RE = re.compile(
    r"\(\s*[A-Za-z_\"`][\w.\"`]*\s*,\s*[A-Za-z_\"`][\w.\"`]*"
    r"(?:\s*,\s*[A-Za-z_\"`][\w.\"`]*)*\s*\)\s*"
    r"(?:=\s*ANY\s*\(\s*%s\s*\)|IN\s*\(\s*%s\s*\))",
    re.IGNORECASE,
)
_ANY_IN_FILTER_RE = re.compile(
    r'([A-Za-z_\"`][\w.\"`]*)\s*(?:=\s*ANY\s*\(\s*%s\s*\)|IN\s*\(\s*%s\s*\))',
    re.IGNORECASE,
)


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


def _split_top_level_and(clause: str) -> List[str]:
    items: List[str] = []
    current: List[str] = []
    depth = 0
    quote: Optional[str] = None
    i = 0
    n = len(clause)
    while i < n:
        c = clause[i]
        if quote is not None:
            if c == quote:
                if i + 1 < n and clause[i + 1] == quote:
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
        elif depth == 0 and clause[i : i + 3].upper() == "AND":
            before = clause[i - 1] if i > 0 else " "
            after = clause[i + 3] if i + 3 < n else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                items.append("".join(current))
                current = []
                i += 3
                continue
            current.append(c)
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


def _scan_top_level_semicolon(sql: str, start: int) -> int:
    """Return the index of the first ``;`` at paren-depth 0, or -1."""
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
        elif c == ")":
            depth -= 1
        elif c == ";" and depth == 0:
            return i
        i += 1
    return -1


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


def find_select_columns(sql: str) -> Optional[List[str]]:
    """Return normalized column names of the first top-level ``SELECT`` list.

    Returns ``None`` when the statement has ``SELECT *`` / a qualified star, no
    top-level ``FROM``, or an empty/unparseable select list.
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
    columns: List[str] = []
    for item in items:
        normalized = re.sub(r"^(DISTINCT|ALL)\s+", "", item, flags=re.IGNORECASE).strip()
        if normalized == "*" or _QUALIFIED_STAR_RE.match(normalized):
            return None
        normalized = _SELECT_AS_ALIAS_RE.sub("", normalized).strip()
        normalized = _SELECT_BARE_ALIAS_RE.sub("", normalized).strip()
        columns.append(_normalize_column(normalized))
    return columns


class _FunctionAnalyzer:
    def __init__(self, function_name: str, module_dicts: Dict[str, Any]) -> None:
        self.function_name = function_name
        self.module_dicts = module_dicts
        self.local_vars: Dict[str, Any] = {}
        self.exact_vars: Dict[str, bool] = {}
        # AST provenance for plain ``x = <string-building expr>`` assignments so
        # placeholder arithmetic can follow names back to their expressions.
        self.sql_ast_vars: Dict[str, ast.AST] = {}
        self.sql_ast_ambiguous: set = set()
        self.function_node: Optional[ast.AST] = None
        self.last_sql: Optional[str] = None
        self.row_bindings: Dict[str, str] = {}
        self.row_reads: Dict[str, List[tuple]] = {}
        self.sql_candidates: List[tuple] = []
        self.execute_sqls: List[tuple] = []
        self.execute_calls: List[tuple] = []
        self.unknown_query_keys: List[tuple] = []
        self.lookup_dicts: List[tuple] = []

    def eval(self, node: Optional[ast.AST]) -> Any:
        return resolve(node, self.local_vars, self.module_dicts)

    def is_exact(self, node: Optional[ast.AST]) -> bool:
        """Whether a SQL expression is a pure constant with no interpolation.

        A ``Name`` is resolved through its assignment provenance so that an
        f-string/``.replace``/``.join`` bound to a variable is still treated as
        interpolated (its non-constant fields are invisible in the resolved
        string, so the placeholder count would be unreliable).
        """
        if node is None:
            return True
        if isinstance(node, ast.Name):
            return self.exact_vars.get(node.id, False)
        for child in ast.walk(node):
            if isinstance(child, (ast.JoinedStr, ast.Call)):
                return False
        return True

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
                        if isinstance(resolved, str):
                            self.exact_vars[stmt.target.id] = self.is_exact(stmt.value)
                        self._record_sql_ast(stmt.target.id, stmt.value)
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
                self.execute_calls.append(
                    (
                        sql if isinstance(sql, str) else None,
                        stmt.lineno,
                        call.args[0],
                        call.args[1] if len(call.args) > 1 else None,
                        call.func.attr,
                    )
                )
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
                if isinstance(resolved, str) and not is_fetch:
                    self.exact_vars[target.id] = self.is_exact(value)
                if not is_fetch:
                    self._record_sql_ast(target.id, value)
        self.collect_row_reads(value)
        self._process_row_unpack(stmt)

    def _record_sql_ast(self, name: str, value: ast.AST) -> None:
        """Track the expression bound to *name*; drop it if rebound."""
        if name in self.sql_ast_ambiguous:
            return
        if name in self.sql_ast_vars:
            # Rebinding makes the value at execute() time ambiguous.
            self.sql_ast_ambiguous.add(name)
            self.sql_ast_vars.pop(name, None)
            return
        self.sql_ast_vars[name] = value

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
        if isinstance(comp, ast.DictComp):
            self.lookup_dicts.append((sql, comp.lineno))
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


def _iter_analyzed(source: str, analyzer_cls: type = _FunctionAnalyzer):
    """Yield ``(function_name, analyzer)`` for each function in *source*."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    module_dicts = collect_module_dicts(tree)
    for name, node in _iter_functions(tree):
        analyzer = analyzer_cls(name, module_dicts)
        analyzer.function_node = node
        analyzer.analyze(node.body)
        yield name, analyzer


def row_index_violations(source: str) -> List[dict]:
    """Return row index reads that exceed the resolved SELECT column count."""
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
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
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql, line in analyzer.sql_candidates:
            if not _CROSS_JOIN_DERIVED_RE.search(sql):
                continue
            key = (name, sql)
            if key in seen:
                continue
            seen.add(key)
            violations.append({"function": name, "sql": sql, "line": line})
    return violations


def implicit_join_sql(source: str) -> List[dict]:
    """Resolved SQL whose top-level FROM list comma-joins 3+ relations."""
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql, line in analyzer.execute_sqls:
            if SENTINEL in sql:
                continue
            from_pos = _scan_keyword(sql, "FROM", 0, 0)
            if from_pos < 0:
                continue
            end = len(sql)
            for keyword in _FROM_CLAUSE_END_KEYWORDS:
                pos = _scan_keyword(sql, keyword, from_pos + 4, 0)
                if pos >= 0 and pos < end:
                    end = pos
            semicolon = _scan_top_level_semicolon(sql, from_pos + 4)
            if semicolon >= 0 and semicolon < end:
                end = semicolon
            from_clause = sql[from_pos + 4 : end]
            relations = len([item for item in _split_top_level(from_clause) if item.strip()])
            if relations < _IMPLICIT_JOIN_MIN_RELATIONS:
                continue
            key = (name, sql)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {"function": name, "sql": sql, "line": line, "relations": relations}
            )
    return violations


def multi_statement_sql(source: str) -> List[dict]:
    """Resolved execute() SQL strings containing >1 top-level statement."""
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
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
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for key, line in analyzer.unknown_query_keys:
            dedup_key = (name, key, line)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            violations.append({"function": name, "key": key, "line": line})
    return violations


def duplicate_where_sql(source: str) -> List[dict]:
    """Resolved SQL strings with 2+ top-level WHERE clauses."""
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
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


def _placeholder_count(sql: str) -> tuple:
    cleaned = sql.replace("%%", "")
    return (
        len(_PLACEHOLDER_POSITIONAL_RE.findall(cleaned)),
        set(_PLACEHOLDER_NAMED_RE.findall(cleaned)),
    )


def _static_params_count(node: Optional[ast.AST]) -> Optional[int]:
    if node is None:
        return 0
    if isinstance(node, ast.Constant) and node.value is None:
        return 0
    if isinstance(node, (ast.List, ast.Tuple)):
        if any(isinstance(elt, ast.Starred) for elt in node.elts):
            return None
        return len(node.elts)
    return None


def placeholder_param_mismatch_sql(source: str) -> List[dict]:
    """Resolved execute() SQL whose static placeholder count differs from its params."""
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql, line, sql_node, params_node, method in analyzer.execute_calls:
            if method != "execute":
                continue
            if not isinstance(sql, str) or SENTINEL in sql:
                continue
            if not analyzer.is_exact(sql_node):
                continue
            positional, named = _placeholder_count(sql)
            if named:
                if not isinstance(params_node, ast.Dict):
                    continue
                expected = len(named)
                actual = sum(1 for key in params_node.keys if key is not None)
            else:
                actual = _static_params_count(params_node)
                if actual is None:
                    continue
                expected = positional
            if expected == actual:
                continue
            key = (name, sql, line)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {
                    "function": name,
                    "sql": sql,
                    "line": line,
                    "placeholders": expected,
                    "params": actual,
                }
            )
    return violations


class _Affine:
    """Affine count ``const + Σ coeff*len(symbol)`` of placeholders/params.

    Symbol keys are ``frozenset`` sets of candidate iterable names: a plain
    iterable yields one candidate, ``zip(A, B)`` yields both and is resolved
    later against the SQL side's symbols.
    """

    __slots__ = ("const", "syms")

    def __init__(self, const: int = 0, syms: Optional[Dict[frozenset, int]] = None) -> None:
        self.const = const
        self.syms = dict(syms or {})


def _affine_add(left: _Affine, right: _Affine) -> _Affine:
    out = _Affine(left.const + right.const, left.syms)
    for key, coeff in right.syms.items():
        out.syms[key] = out.syms.get(key, 0) + coeff
    return out


# Builtins that only wrap an iterable without changing its length/identity.
_ITER_WRAPPERS = {"range", "len", "list", "tuple", "set", "sorted", "reversed", "iter"}
_LIST_MUTATIONS = {"append", "extend", "insert", "pop", "remove", "clear", "sort", "reverse"}


def _iter_symbols(node: Optional[ast.AST]) -> Optional[frozenset]:
    """Normalised identity of the iterable an expression iterates over.

    ``len(X)`` / ``range(len(X))`` / ``sorted(X)`` all normalise to ``X``;
    ``zip(A, B)`` yields both candidates so the caller can pick the one that
    also appears on the SQL side.
    """
    if isinstance(node, ast.Name):
        return frozenset({node.id})
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in _ITER_WRAPPERS and len(node.args) == 1:
            return _iter_symbols(node.args[0])
        if node.func.id == "zip":
            symbols: set = set()
            for arg in node.args:
                sub = _iter_symbols(arg)
                if sub is None:
                    return None
                symbols |= sub
            return frozenset(symbols) if symbols else None
    return None


def _is_len_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
        and len(node.args) == 1
    )


def _join_pattern(node: ast.AST) -> Optional[str]:
    if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) == 1:
        node = node.elts[0]
    return _const_str(node)


def _join_affine(seq: ast.AST) -> Optional[_Affine]:
    """Placeholder count of a ``join([pattern] * len(X))``-style fragment."""
    pattern: Optional[str] = None
    iterable: Optional[ast.AST] = None
    if isinstance(seq, ast.BinOp) and isinstance(seq.op, ast.Mult):
        if _is_len_call(seq.right):
            pattern, iterable = _join_pattern(seq.left), seq.right.args[0]
        elif _is_len_call(seq.left):
            pattern, iterable = _join_pattern(seq.right), seq.left.args[0]
    elif isinstance(seq, ast.ListComp) and len(seq.generators) == 1:
        generator = seq.generators[0]
        if not generator.ifs:
            pattern, iterable = _const_str(seq.elt), generator.iter
    if pattern is None or iterable is None:
        return None
    symbols = _iter_symbols(iterable)
    if not symbols:
        return None
    return _Affine(0, {symbols: _placeholder_count(pattern)[0]})


def _sql_affine(
    node: Optional[ast.AST], sql_vars: Dict[str, ast.AST], depth: int = 0
) -> Optional[_Affine]:
    """Symbolic placeholder count of the SQL expression passed to execute()."""
    if node is None or depth > 8:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _Affine(_placeholder_count(node.value)[0])
    if isinstance(node, ast.JoinedStr):
        total = _Affine()
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                total.const += _placeholder_count(part.value)[0]
            elif isinstance(part, ast.FormattedValue):
                fragment = _sql_affine(part.value, sql_vars, depth + 1)
                if fragment is None:
                    return None
                total = _affine_add(total, fragment)
            else:
                return None
        return total
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _sql_affine(node.left, sql_vars, depth + 1)
        right = _sql_affine(node.right, sql_vars, depth + 1)
        if left is None or right is None:
            return None
        return _affine_add(left, right)
    if isinstance(node, ast.Call) and len(node.args) == 1:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "join":
            return _join_affine(node.args[0])
    if isinstance(node, ast.Name):
        stored = sql_vars.get(node.id)
        if stored is None:
            return None
        return _sql_affine(stored, sql_vars, depth + 1)
    return None


def _literal_arity(node: ast.AST) -> Optional[int]:
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return None
    if any(isinstance(elt, ast.Starred) for elt in node.elts):
        return None
    return len(node.elts)


def _assigned_list_affine(value: ast.AST) -> Optional[_Affine]:
    """Symbolic length of a list bound by a plain assignment."""
    arity = _literal_arity(value)
    if arity is not None:
        return _Affine(arity)
    if isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        if len(value.generators) == 1:
            generator = value.generators[0]
            if generator.ifs:
                return None
            arity = _literal_arity(value.elt)
            symbols = _iter_symbols(generator.iter)
            if arity is None or not symbols:
                return None
            return _Affine(0, {symbols: arity})
        if len(value.generators) == 2:
            # Flatten: [x for t in TS for x in (a, b, c)] is 3*len(TS).
            first, second = value.generators
            if first.ifs or second.ifs:
                return None
            width = _literal_arity(second.iter)
            symbols = _iter_symbols(first.iter)
            if width is None or not symbols:
                return None
            return _Affine(0, {symbols: width})
    return None


def _param_list_affines(function: Optional[ast.AST]) -> Dict[str, Optional[_Affine]]:
    """Symbolic lengths of the parameter lists built inside *function*.

    A name maps to ``None`` as soon as its construction is not fully
    understood (mutated conditionally, inside nested loops, reassigned, or by
    an unrecognised method), so callers bail instead of guessing.
    """
    counts: Dict[str, Optional[_Affine]] = {}
    if function is None:
        return counts

    def record(name: str, value: Optional[_Affine]) -> None:
        counts[name] = None if name in counts else value

    def mutate(call: ast.Call, loops: tuple, conditional: bool) -> None:
        func = call.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            return
        name = func.value.id
        if name not in counts or func.attr not in _LIST_MUTATIONS:
            return
        if func.attr not in {"append", "extend"} or conditional or len(loops) > 1:
            counts[name] = None
            return
        if len(call.args) != 1:
            counts[name] = None
            return
        arity = _literal_arity(call.args[0])
        current = counts[name]
        if arity is None or current is None:
            counts[name] = None
            return
        if loops:
            symbols = loops[-1]
            if not symbols:
                counts[name] = None
                return
            current.syms[symbols] = current.syms.get(symbols, 0) + arity
        else:
            current.const += arity

    def scan(body: List[ast.stmt], loops: tuple, conditional: bool) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        record(target.id, _assigned_list_affine(stmt.value))
            elif isinstance(stmt, ast.AnnAssign):
                if isinstance(stmt.target, ast.Name) and stmt.value is not None:
                    record(stmt.target.id, _assigned_list_affine(stmt.value))
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                mutate(stmt.value, loops, conditional)
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                scan(stmt.body, loops + (_iter_symbols(stmt.iter),), conditional)
                scan(stmt.orelse, loops, conditional)
            elif isinstance(stmt, ast.While):
                scan(stmt.body, loops, True)
                scan(stmt.orelse, loops, True)
            elif isinstance(stmt, ast.If):
                scan(stmt.body, loops, True)
                scan(stmt.orelse, loops, True)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                scan(stmt.body, loops, conditional)
            elif isinstance(stmt, ast.Try):
                scan(stmt.body, loops, True)
                scan(stmt.orelse, loops, True)
                scan(stmt.finalbody, loops, True)
                for handler in stmt.handlers:
                    scan(handler.body, loops, True)

    scan(function.body, (), False)  # type: ignore[attr-defined]
    return counts


def _params_affine(
    node: Optional[ast.AST], counts: Dict[str, Optional[_Affine]]
) -> Optional[_Affine]:
    if node is None:
        return _Affine(0)
    if isinstance(node, ast.Constant) and node.value is None:
        return _Affine(0)
    if isinstance(node, (ast.List, ast.Tuple)):
        arity = _literal_arity(node)
        return None if arity is None else _Affine(arity)
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return _assigned_list_affine(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        # [scalar] + flat_params: hoisted scalars plus a batched list.
        left = _params_affine(node.left, counts)
        right = _params_affine(node.right, counts)
        if left is None or right is None:
            return None
        return _affine_add(left, right)
    if isinstance(node, ast.Name):
        return counts.get(node.id)
    return None


def _resolve_symbols(affine: _Affine, available: set) -> Optional[Dict[str, int]]:
    resolved: Dict[str, int] = {}
    for candidates, coeff in affine.syms.items():
        matches = candidates & available
        if len(matches) != 1:
            return None
        name = next(iter(matches))
        resolved[name] = resolved.get(name, 0) + coeff
    return resolved


def _affine_text(const: int, syms: Dict[str, int]) -> str:
    parts = [str(const)] if const or not syms else []
    for name in sorted(syms):
        coeff = syms[name]
        parts.append(f"len({name})" if coeff == 1 else f"{coeff}*len({name})")
    return " + ".join(parts)


def _single_symbols(affine: _Affine) -> Optional[Dict[str, int]]:
    syms: Dict[str, int] = {}
    for candidates, coeff in affine.syms.items():
        if len(candidates) != 1:
            return None
        name = next(iter(candidates))
        syms[name] = syms.get(name, 0) + coeff
    return syms


def dynamic_placeholder_arity_sql(source: str) -> List[dict]:
    """execute() calls whose placeholder/param counts only agree at one row.

    Resolves the batched-key pattern symbolically::

        placeholders = ",".join(["(%s, %s, %s)"] * len(keys))   # 3*len(keys)
        params = []
        for k in keys:
            params.extend([a, k, b, c])                          # 4*len(keys)

    Equal only for a single element, so a batch of two or more raises at
    runtime (psycopg2 applies ``%`` itself). Anything the analyzer cannot
    resolve exactly — mismatched symbol sets, unknown fragments, conditional
    mutation — is skipped rather than guessed.
    """
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        counts = _param_list_affines(analyzer.function_node)
        for _sql, line, sql_node, params_node, method in analyzer.execute_calls:
            if method != "execute":
                continue
            sql_affine = _sql_affine(sql_node, analyzer.sql_ast_vars)
            if sql_affine is None or not sql_affine.syms:
                # Exact/static SQL stays the job of placeholder_param_mismatch_sql.
                continue
            sql_syms = _single_symbols(sql_affine)
            if not sql_syms:
                continue
            params_affine = _params_affine(params_node, counts)
            if params_affine is None:
                continue
            params_syms = _resolve_symbols(params_affine, set(sql_syms))
            if params_syms is None or set(params_syms) != set(sql_syms):
                continue
            if sql_affine.const == params_affine.const and sql_syms == params_syms:
                continue
            key = (name, line)
            if key in seen:
                continue
            seen.add(key)
            shape = " ".join(ast.unparse(sql_node).split())
            if len(shape) > 160:
                shape = shape[:157] + "..."
            violations.append(
                {
                    "function": name,
                    "line": line,
                    "sql_shape": shape,
                    "placeholders": _affine_text(sql_affine.const, sql_syms),
                    "params": _affine_text(params_affine.const, params_syms),
                }
            )
    return violations


_VALUES_TAIL_RE = re.compile(r"\bVALUES\s*$", re.I)
_VALUES_ALIAS_RE = re.compile(r"^\s*\)\s*AS\s+[\w\".]+\s*\(([^)]*)\)", re.I | re.S)


def _fstring_parts(
    node: Optional[ast.AST], sql_vars: Dict[str, ast.AST], depth: int = 0
) -> Optional[List[tuple]]:
    """Flatten a SQL expression into ``("text", str)`` / ``("expr", node)`` parts."""
    if node is None or depth > 8:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [("text", node.value)]
    if isinstance(node, ast.JoinedStr):
        parts: List[tuple] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(("text", part.value))
            elif isinstance(part, ast.FormattedValue):
                parts.append(("expr", part.value))
            else:
                return None
        return parts
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fstring_parts(node.left, sql_vars, depth + 1)
        right = _fstring_parts(node.right, sql_vars, depth + 1)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.Name):
        stored = sql_vars.get(node.id)
        return None if stored is None else _fstring_parts(stored, sql_vars, depth + 1)
    return [("expr", node)]


def values_alias_arity_sql(source: str) -> List[dict]:
    """``VALUES`` rows whose width disagrees with the derived-table column list.

    Catches the batched-write shape::

        f"... FROM (VALUES {rows}) AS v(c_id, c_d_id, c_w_id, ol_total) ..."

    where ``rows`` expands to ``(%s, %s, %s)`` per row. Postgres rejects this
    for every input ("table has 3 columns available but 4 columns specified"),
    so the transaction fails deterministically at runtime.
    """
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for _sql, line, sql_node, _params, method in analyzer.execute_calls:
            if method != "execute":
                continue
            parts = _fstring_parts(sql_node, analyzer.sql_ast_vars)
            if not parts:
                continue
            for i, (kind, value) in enumerate(parts):
                if kind != "expr":
                    continue
                prev = parts[i - 1][1] if i and parts[i - 1][0] == "text" else ""
                nxt = parts[i + 1][1] if i + 1 < len(parts) and parts[i + 1][0] == "text" else ""
                if not _VALUES_TAIL_RE.search(prev):
                    continue
                match = _VALUES_ALIAS_RE.match(nxt)
                if match is None:
                    continue
                columns = [col for col in match.group(1).split(",") if col.strip()]
                if not columns:
                    continue
                affine = _sql_affine(value, analyzer.sql_ast_vars)
                if affine is None or affine.const or len(affine.syms) != 1:
                    continue
                width = next(iter(affine.syms.values()))
                if width == len(columns):
                    continue
                key = (name, line)
                if key in seen:
                    continue
                seen.add(key)
                shape = " ".join(nxt.split())
                if len(shape) > 160:
                    shape = shape[:157] + "..."
                violations.append(
                    {
                        "function": name,
                        "line": line,
                        "sql_shape": shape,
                        "row_width": width,
                        "alias_columns": len(columns),
                    }
                )
    return violations


def _element_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript):
        return _const_str(node.slice)
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _tuple_element_names(node: Optional[ast.AST]) -> Optional[tuple]:
    if isinstance(node, (ast.Tuple, ast.List)) and node.elts:
        names = [_element_name(elt) for elt in node.elts]
        if all(name is not None for name in names):
            return tuple(names)
    return None


def _param_tuple_names(
    node: Optional[ast.AST], orders: Dict[str, tuple]
) -> Optional[tuple]:
    """Element names of the tuple list flattened into a parameter expression."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return orders.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _param_tuple_names(node.right, orders) or _param_tuple_names(node.left, orders)
    if isinstance(node, ast.ListComp) and len(node.generators) == 2:
        first, second = node.generators
        if (
            isinstance(first.target, ast.Name)
            and isinstance(second.iter, ast.Name)
            and second.iter.id == first.target.id
        ):
            return _iter_tuple_names(first.iter, orders)
    return None


def _iter_tuple_names(
    node: Optional[ast.AST], orders: Dict[str, tuple]
) -> Optional[tuple]:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return orders.get(node.id)
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        shapes = {_tuple_element_names(elt) for elt in node.elts}
        if len(shapes) == 1 and None not in shapes:
            return shapes.pop()
        return None
    if isinstance(node, ast.ListComp) and len(node.generators) == 1:
        generator = node.generators[0]
        if generator.ifs:
            return None
        direct = _tuple_element_names(node.elt)
        if direct is not None:
            return direct
        base = _iter_tuple_names(generator.iter, orders)
        if base is None or not isinstance(node.elt, ast.Tuple) or not isinstance(generator.target, ast.Name):
            return None
        projected = []
        for elt in node.elt.elts:
            if (
                isinstance(elt, ast.Subscript)
                and isinstance(elt.value, ast.Name)
                and elt.value.id == generator.target.id
                and isinstance(elt.slice, ast.Constant)
                and isinstance(elt.slice.value, int)
                and 0 <= elt.slice.value < len(base)
            ):
                projected.append(base[elt.slice.value])
            else:
                return None
        return tuple(projected)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "zip":
        if all(isinstance(arg, ast.Name) for arg in node.args):
            return tuple(arg.id for arg in node.args)
    return None


def _list_tuple_orders(function: Optional[ast.AST]) -> Dict[str, tuple]:
    """Tuple element order of every list built by append/extend in *function*."""
    if function is None:
        return {}
    orders: Dict[str, tuple] = {}
    ambiguous: set = set()

    def record(name: str, names: tuple) -> None:
        if name in ambiguous:
            return
        if name in orders:
            if orders[name] != names:
                ambiguous.add(name)
                orders.pop(name, None)
            return
        orders[name] = names

    def collect_direct() -> None:
        for stmt in ast.walk(function):
            target = None
            value = None
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                target = stmt.targets[0].id
                value = stmt.value
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if (
                    isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.attr in {"append", "extend"}
                    and len(call.args) == 1
                ):
                    target = call.func.value.id
                    value = call.args[0]
            if target is None or value is None:
                continue
            names = _tuple_element_names(value)
            if names is None:
                names = _iter_tuple_names(value, orders)
            if names is None:
                names = _param_tuple_names(value, orders)
            if names is not None:
                record(target, names)

    def collect_loop_bindings() -> bool:
        """``for t in rows:`` where ``rows`` has a known tuple shape."""
        changed = False
        for node in ast.walk(function):
            if not isinstance(node, (ast.For, ast.AsyncFor)):
                continue
            if not isinstance(node.target, ast.Name) or node.target.id in orders:
                continue
            names = _iter_tuple_names(node.iter, orders)
            if names is not None:
                orders[node.target.id] = names
                changed = True
        return changed

    collect_direct()
    while collect_loop_bindings():
        collect_direct()
    return orders


_COMPOSITE_IN_RE = re.compile(
    r"\(\s*([A-Za-z_][\w.]*\s*(?:,\s*[A-Za-z_][\w.]*\s*)+)\)\s+IN\s*\(",
    re.I,
)


def _name_matches_column(name: str, column: str) -> bool:
    name = name.lower()
    column = column.lower()
    if name == column:
        return True
    if len(name) >= 3 and name in column:
        return True
    if len(column) >= 3 and column in name:
        return True
    for value, other in ((name, column), (column, name)):
        if "_" in value:
            tail = value.split("_", 1)[1]
            if len(tail) >= 3 and tail in other:
                return True
    return False


def _swapped_order(names: tuple, columns: List[str]):
    """``(used_names, expected_columns)`` when a unique non-identity match exists."""
    if len(names) < len(columns) or not columns:
        return None
    candidates = []
    for offset in (len(names) - len(columns), 0):
        window = list(names[offset : offset + len(columns)])
        matches = [
            [j for j, col in enumerate(columns) if _name_matches_column(n, col)]
            for n in window
        ]
        solutions = [
            perm
            for perm in permutations(range(len(columns)))
            if all(perm[i] in matches[i] for i in range(len(columns)))
        ]
        if len(solutions) != 1:
            continue
        if solutions[0] == tuple(range(len(columns))):
            return None
        candidates.append((window, [columns[j] for j in solutions[0]]))
    if not candidates:
        return None
    expected = candidates[0][1]
    if any(candidate[1] != expected for candidate in candidates[1:]):
        return None
    return candidates[0]


def _where_equality_columns(tree, exp) -> List[str]:
    where = tree.args.get("where")
    if where is None or where.find(exp.Or) is not None or where.find(exp.In) is not None:
        return []
    # ``find_all`` does not preserve source order; parse the rendered WHERE
    # clause instead so tuple positions line up with placeholder positions.
    where_sql = where.sql(dialect="postgres")
    return [
        match.group(1).split(".")[-1].strip('"`').lower()
        for match in re.finditer(
            r"([A-Za-z_][\w.\"]*)\s*=\s*(?:0|%s|\?|:\w+)", where_sql
        )
    ]


def batched_write_tuple_order_sql(source: str) -> List[dict]:
    """``execute_batch`` point writes whose tuple order mismatches the WHERE keys.

    Catches reusing the ``ORDERS`` key order ``(no_o_id, d_id, w_id)`` for
    ``DELETE FROM NEW_ORDER ... WHERE NO_D_ID, NO_W_ID, NO_O_ID``: the delete
    silently removes nothing while the carrier update succeeds, leaving
    delivered rows in ``NEW_ORDER`` (CC5).
    """
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:  # pragma: no cover - declared dependency
        return []

    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        orders = _list_tuple_orders(analyzer.function_node)
        for node in ast.walk(analyzer.function_node):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr in {"execute_batch", "executemany"}
                and len(node.args) >= 3
            ):
                continue
            sql = analyzer.eval(node.args[1])
            if not isinstance(sql, str):
                sql = _template_sql(node.args[1], analyzer.sql_ast_vars)
            if not isinstance(sql, str):
                continue
            normalized = sql.replace("%s", "0").replace("%%", "%")
            try:
                tree = sqlglot.parse_one(normalized, read="postgres")
            except Exception:
                continue
            if tree is None:
                continue
            if len(list(tree.find_all(exp.Table))) != 1:
                continue
            if list(tree.find_all(exp.Subquery)):
                continue
            columns = _where_equality_columns(tree, exp)
            if len(columns) < 2 or len(columns) > 6:
                continue
            if any(not re.fullmatch(r"[a-z_]\w*", col) for col in columns):
                continue
            names = _param_tuple_names(node.args[2], orders)
            if names is None:
                continue
            swapped = _swapped_order(names, columns)
            if swapped is None:
                continue
            used_names, expected = swapped
            key = (name, getattr(node, "lineno", 0))
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {
                    "function": name,
                    "line": getattr(node, "lineno", 0),
                    "columns": columns,
                    "tuple_order": used_names,
                    "expected_order": expected,
                }
            )
    return violations


def composite_tuple_order_sql(source: str) -> List[dict]:
    """Composite ``(col, ...) IN ((%s, ...), ...)`` whose tuple order is swapped.

    Resolves the list appended as ``(d_id, no_o_id)`` and compares it with the
    SQL column tuple ``(O_ID, O_D_ID)``. A perfect name match in a different
    order means the batched predicate matches nothing: the lookup dict is empty
    and the next index/assert fails. Ambiguous names are skipped.
    """
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        orders = _list_tuple_orders(analyzer.function_node)
        for sql, line, _sql_node, params_node, method in analyzer.execute_calls:
            if method != "execute" or not isinstance(sql, str):
                continue
            found = list(_COMPOSITE_IN_RE.finditer(sql))
            if len(found) != 1:
                continue
            columns = [
                col.strip().split(".")[-1].strip('"`').lower()
                for col in found[0].group(1).split(",")
            ]
            if (
                len(columns) < 2
                or len(columns) > 6
                or any(not re.fullmatch(r"[a-z_]\w*", col) for col in columns)
            ):
                continue
            names = _param_tuple_names(params_node, orders)
            if names is None or len(names) != len(columns):
                continue
            matches = [
                [j for j, col in enumerate(columns) if _name_matches_column(n, col)]
                for n in names
            ]
            solutions = [
                perm
                for perm in permutations(range(len(columns)))
                if all(perm[i] in matches[i] for i in range(len(columns)))
            ]
            if len(solutions) != 1 or solutions[0] == tuple(range(len(columns))):
                continue
            key = (name, line)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {
                    "function": name,
                    "line": line,
                    "columns": columns,
                    "tuple_order": list(names),
                    "expected_order": [columns[j] for j in solutions[0]],
                }
            )
    return violations


def _width_from_fetch(
    value: ast.AST,
    bindings: Dict[str, set],
    executes: List[tuple],
    lineno: int,
) -> Optional[set]:
    if _is_fetch_call(value):
        previous = [sql for line, sql in executes if line < lineno]
        if not previous:
            return None
        width = find_select_column_count(previous[-1])
        return None if width is None else {width}
    if isinstance(value, ast.Name):
        widths = bindings.get(value.id)
        return set(widths) if widths else None
    if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name):
        widths = bindings.get(value.value.id)
        if not widths:
            return None
        slice_node = value.slice
        if not isinstance(slice_node, ast.Slice):
            # ``rows[index]`` selects a row; the row width is unchanged.
            return set(widths)
        lower = 0 if slice_node.lower is None else _const_int(slice_node.lower)
        upper = None if slice_node.upper is None else _const_int(slice_node.upper)
        if lower is None or (slice_node.upper is not None and upper is None):
            return None
        if slice_node.step is not None:
            step = _const_int(slice_node.step)
            if step != 1:
                return None
        sliced: set = set()
        for width in widths:
            high = width if upper is None else upper
            if 0 <= lower <= high <= width:
                sliced.add(high - lower)
            else:
                return None
        return sliced
    if isinstance(value, (ast.Tuple, ast.List)):
        # A constructed row has exactly one element per position. An empty
        # literal is the no-rows fallback, not a row shape: skip it.
        if not value.elts or any(isinstance(elt, ast.Starred) for elt in value.elts):
            return None
        return {len(value.elts)}
    if isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        if len(value.generators) != 1 or value.generators[0].ifs:
            return None
        gen = value.generators[0]
        iter_widths = _width_from_fetch(gen.iter, bindings, executes, lineno)
        if not iter_widths:
            return None
        inner = dict(bindings)
        targets = [n.id for n in ast.walk(gen.target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]
        for target in targets:
            inner[target] = set(iter_widths)
        return _width_from_fetch(value.elt, inner, executes, lineno)
    return None


def _write_verb_table(sql: str):
    """(VERB, TABLE) for a single DML statement, else None.

    Uses sqlglot locally so this module stays independent of db_interaction.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return None
    if not isinstance(sql, str) or not sql.strip():
        return None
    normalized = sql.replace("%s", "0").replace("%%", "%")
    try:
        tree = sqlglot.parse_one(normalized, read="postgres")
    except Exception:
        return None
    if tree is None or not isinstance(tree, (exp.Insert, exp.Update, exp.Delete)):
        return None
    target = tree.args.get("this")
    table = ""
    if isinstance(target, exp.Table):
        table = target.name or ""
    elif isinstance(target, exp.Schema) and isinstance(target.this, exp.Table):
        table = target.this.name or ""
    if not table:
        return None
    verb = "INSERT" if isinstance(tree, exp.Insert) else "UPDATE" if isinstance(tree, exp.Update) else "DELETE"
    return (verb, table.upper())


def _batch_sql_params(node: ast.Call):
    """(sql_node, params_node) for execute/executemany/execute_batch."""
    if isinstance(node.func, ast.Attribute):
        attr = node.func.attr
    elif isinstance(node.func, ast.Name):
        attr = node.func.id
    else:
        return None
    if attr in {"execute", "executemany"} and len(node.args) >= 1:
        return (node.args[0], node.args[1] if len(node.args) > 1 else None)
    if attr in {"execute_batch", "execute_values"} and len(node.args) >= 3:
        return (node.args[1], node.args[2])
    return None


def write_tables_sql(source: str) -> Dict[str, set]:
    """(VERB, TABLE) writes per function, including execute_batch.

    The existing execute-call model only records execute/executemany, so
    batched writes are invisible to it. This walks every call itself and
    resolves the SQL through the analyzer env, so ``q["deleteNewOrder"]``
    dict lookups keep working. Sets (not counts) are returned: batching
    legitimately collapses N loop writes into one call.
    """
    result: Dict[str, set] = {}
    for name, analyzer in _iter_analyzed(source):
        function = analyzer.function_node
        if function is None:
            continue
        writes: set = set()
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                attr = node.func.attr
            elif isinstance(node.func, ast.Name):
                attr = node.func.id
            else:
                continue
            if attr not in {"execute", "executemany", "execute_batch", "execute_values"}:
                continue
            pair = _batch_sql_params(node)
            if pair is None:
                continue
            sql = analyzer.eval(pair[0])
            if not isinstance(sql, str):
                sql = _template_sql(pair[0], analyzer.sql_ast_vars)
            if not isinstance(sql, str):
                continue
            verb_table = _write_verb_table(sql)
            if verb_table is not None:
                writes.add(verb_table)
        if writes:
            result[name] = writes
    return result


def _producer_sqls(value: ast.AST, bindings: Dict[str, set], executes: List[tuple], lineno: int):
    if _is_fetch_call(value):
        previous = [sql for line, sql in executes if line < lineno]
        return {previous[-1]} if previous else None
    if isinstance(value, ast.Name):
        sqls = bindings.get(value.id)
        return set(sqls) if sqls else None
    if isinstance(value, ast.Subscript):
        # Indexing/slicing a fetched row does not change its producer query.
        return _producer_sqls(value.value, bindings, executes, lineno)
    if isinstance(value, ast.Starred):
        return _producer_sqls(value.value, bindings, executes, lineno)
    if isinstance(value, (ast.Tuple, ast.List)):
        out: set = set()
        for elt in value.elts:
            sqls = _producer_sqls(elt, bindings, executes, lineno)
            if sqls is None:
                return None
            out |= sqls
        return out
    if isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        if len(value.generators) != 1 or value.generators[0].ifs:
            return None
        gen = value.generators[0]
        iter_sqls = _producer_sqls(gen.iter, bindings, executes, lineno)
        if not iter_sqls:
            return None
        inner = dict(bindings)
        targets = [n.id for n in ast.walk(gen.target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]
        for target in targets:
            inner[target] = set(iter_sqls)
        return _producer_sqls(value.elt, inner, executes, lineno)
    if isinstance(value, ast.DictComp):
        if len(value.generators) != 1 or value.generators[0].ifs:
            return None
        gen = value.generators[0]
        iter_sqls = _producer_sqls(gen.iter, bindings, executes, lineno)
        if not iter_sqls:
            return None
        inner = dict(bindings)
        targets = [n.id for n in ast.walk(gen.target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]
        for target in targets:
            inner[target] = set(iter_sqls)
        key_sqls = _producer_sqls(value.key, inner, executes, lineno)
        val_sqls = _producer_sqls(value.value, inner, executes, lineno)
        if key_sqls is None or val_sqls is None:
            return None
        return key_sqls | val_sqls
    return None


def _select_modifiers(sql: str) -> Optional[Dict[str, bool]]:
    """Whether a SELECT has ORDER BY / LIMIT / JOIN (top level only)."""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return None
    if not isinstance(sql, str) or not sql.strip().upper().startswith("SELECT"):
        return {"order_by": False, "limit": False, "join": False, "group_by": False}
    normalized = sql.replace("%s", "0").replace("%%", "%")
    try:
        tree = sqlglot.parse_one(normalized, read="postgres")
    except Exception:
        upper = normalized.upper()
        return {
            "order_by": "ORDER BY" in upper,
            "limit": "LIMIT" in upper,
            "join": "JOIN" in upper,
            "group_by": "GROUP BY" in upper,
        }
    if tree is None or not isinstance(tree, exp.Select):
        return {"order_by": False, "limit": False, "join": False, "group_by": False}
    joins = tree.args.get("joins") or []
    from_ = tree.args.get("from_")
    comma_sources = len(from_.expressions) if from_ is not None and isinstance(from_, exp.From) else 1
    return {
        "order_by": tree.args.get("order") is not None,
        "limit": tree.args.get("limit") is not None,
        "join": len(joins) > 0 or comma_sources > 1,
        "group_by": tree.args.get("group") is not None,
    }


def returned_select_modifiers_sql(source: str) -> Dict[str, Dict[str, Dict[str, List[bool]]]]:
    """ORDER BY / LIMIT of every fetched row that the function returns.

    Mirrors returned_row_widths_sql but tracks query shape instead of width:
    ``getLastOrder`` (ORDER BY O_ID DESC LIMIT 1) must keep both, otherwise a
    batched rewrite returns an arbitrary row. Maps
    ``function -> returned variable -> {"order_by": [...], "limit": [...]}``
    with sorted unique flags across the variable's producer SELECTs.
    """
    result: Dict[str, Dict[str, Dict[str, List[bool]]]] = {}
    for name, analyzer in _iter_analyzed(source):
        function = analyzer.function_node
        if function is None:
            continue
        executes = sorted(
            (line, sql)
            for sql, line, _node, _params, method in analyzer.execute_calls
            if isinstance(sql, str) and method == "execute"
        )
        bindings: Dict[str, set] = {}
        assigns = [
            node
            for node in ast.walk(function)
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
        ]
        assigns.sort(key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)))
        for node in assigns:
            sqls = _producer_sqls(node.value, bindings, executes, node.lineno)
            if not sqls:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings.setdefault(target.id, set()).update(sqls)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    for element in target.elts:
                        if isinstance(element, ast.Name):
                            bindings.setdefault(element.id, set()).update(sqls)

        returned: set = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Return) and node.value is not None:
                for child in ast.walk(node.value):
                    if isinstance(child, ast.Name):
                        returned.add(child.id)
        shapes: Dict[str, Dict[str, List[bool]]] = {}
        for var in returned:
            if var not in bindings or not bindings[var]:
                continue
            order_flags: set = set()
            limit_flags: set = set()
            join_flags: set = set()
            group_flags: set = set()
            for sql in bindings[var]:
                mods = _select_modifiers(sql)
                if mods is None:
                    continue
                order_flags.add(mods["order_by"])
                limit_flags.add(mods["limit"])
                join_flags.add(mods["join"])
                group_flags.add(mods["group_by"])
            if order_flags or limit_flags or join_flags or group_flags:
                shapes[var] = {
                    "order_by": sorted(order_flags),
                    "limit": sorted(limit_flags),
                    "join": sorted(join_flags),
                    "group_by": sorted(group_flags),
                }
        if shapes:
            result[name] = shapes
    return result


def returned_row_widths_sql(source: str) -> Dict[str, Dict[str, List[int]]]:
    """SELECT width of every fetched row that the function returns.

    ``customer = self.cursor.fetchone()`` then ``return [customer, ...]`` is a
    transaction payload: widening its SELECT projection changes the returned
    row (and the wire output) even though the return statement itself is
    unchanged. Maps ``function -> returned variable -> sorted widths``.
    """
    result: Dict[str, Dict[str, List[int]]] = {}
    for name, analyzer in _iter_analyzed(source):
        function = analyzer.function_node
        if function is None:
            continue
        executes = sorted(
            (line, sql)
            for sql, line, _node, _params, method in analyzer.execute_calls
            if isinstance(sql, str) and method == "execute"
        )
        bindings: Dict[str, set] = {}
        assigns = [
            node
            for node in ast.walk(function)
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
        ]
        assigns.sort(key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)))
        for node in assigns:
            widths = _width_from_fetch(node.value, bindings, executes, node.lineno)
            if widths is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings.setdefault(target.id, set()).update(widths)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    for element in target.elts:
                        if isinstance(element, ast.Name):
                            bindings.setdefault(element.id, set()).update(widths)

        returned: set = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Return) and node.value is not None:
                for child in ast.walk(node.value):
                    if isinstance(child, ast.Name):
                        returned.add(child.id)
        widths_by_var = {
            var: sorted(bindings[var])
            for var in returned
            if var in bindings and bindings[var]
        }
        if widths_by_var:
            result[name] = widths_by_var
    return result


_STATEMENT_PLACEHOLDER_RE = re.compile(
    r"\b(?:AS|FROM|JOIN)\s*\(\s*%(?:s|\(\w+\)s)(?=\s|$|\))",
    re.I,
)


def _contains_mogrify(
    node: Optional[ast.AST], sql_vars: Dict[str, ast.AST], depth: int = 0
) -> bool:
    if node is None or depth > 8:
        return False
    if isinstance(node, ast.Name):
        stored = sql_vars.get(node.id)
        return _contains_mogrify(stored, sql_vars, depth + 1)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "mogrify":
            return True
        return _contains_mogrify(func, sql_vars, depth + 1) or any(
            _contains_mogrify(arg, sql_vars, depth + 1)
            for arg in list(node.args) + [kw.value for kw in node.keywords]
        )
    if isinstance(node, ast.Attribute):
        return _contains_mogrify(node.value, sql_vars, depth + 1)
    if isinstance(node, ast.JoinedStr):
        return any(
            _contains_mogrify(part.value, sql_vars, depth + 1)
            for part in node.values
            if isinstance(part, ast.FormattedValue)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _contains_mogrify(node.left, sql_vars, depth + 1) or _contains_mogrify(
            node.right, sql_vars, depth + 1
        )
    if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
        return _contains_mogrify(node.elt, sql_vars, depth + 1)
    return False


def _is_flatten_comprehension(node: Optional[ast.AST]) -> bool:
    return (
        isinstance(node, ast.ListComp)
        and len(node.generators) == 2
        and isinstance(node.generators[0].target, ast.Name)
        and isinstance(node.generators[1].iter, ast.Name)
        and node.generators[1].iter.id == node.generators[0].target.id
    )


def _params_include_flattening(
    node: Optional[ast.AST], sql_vars: Dict[str, ast.AST], depth: int = 0
) -> bool:
    if node is None or depth > 8:
        return False
    if _is_flatten_comprehension(node):
        return True
    if isinstance(node, ast.Name):
        stored = sql_vars.get(node.id)
        return _is_flatten_comprehension(stored) or _params_include_flattening(
            stored, sql_vars, depth + 1
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _params_include_flattening(
            node.left, sql_vars, depth + 1
        ) or _params_include_flattening(node.right, sql_vars, depth + 1)
    return False


_CONTAINER_MUTATIONS = {
    "append",
    "extend",
    "insert",
    "pop",
    "remove",
    "clear",
    "sort",
    "reverse",
    "update",
    "setdefault",
}


def _mutated_container_names(function: Optional[ast.AST]) -> set:
    mutated: set = set()
    if function is None:
        return mutated
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.attr in _CONTAINER_MUTATIONS
        ):
            mutated.add(node.func.value.id)
        elif (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.op, ast.Add)
        ):
            mutated.add(node.target.id)
    return mutated


def _is_empty_params(
    node: Optional[ast.AST],
    sql_vars: Dict[str, ast.AST],
    mutated: set,
    depth: int = 0,
) -> bool:
    if node is None or depth > 6:
        return False
    if isinstance(node, (ast.List, ast.Tuple)) and not node.elts:
        return True
    if isinstance(node, ast.Dict) and not node.keys:
        return True
    if isinstance(node, ast.IfExp) and isinstance(node.test, ast.Constant):
        branch = node.body if node.test.value else node.orelse
        return _is_empty_params(branch, sql_vars, mutated, depth + 1)
    if isinstance(node, ast.Name):
        if node.id in mutated:
            return False
        return _is_empty_params(sql_vars.get(node.id), sql_vars, mutated, depth + 1)
    return False


def empty_params_with_placeholders_sql(source: str) -> List[dict]:
    """``execute(sql, [])`` where *sql* still contains placeholders.

    psycopg2 indexes the parameter sequence for every placeholder, so an empty
    list raises ``IndexError: list index out of range`` (or a tuple index
    error). Dynamic SQL is checked symbolically.
    """
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        mutated = _mutated_container_names(analyzer.function_node)
        for sql, line, sql_node, params_node, method in analyzer.execute_calls:
            if method != "execute" or not _is_empty_params(
                params_node, analyzer.sql_ast_vars, mutated
            ):
                continue
            affine = _sql_affine(sql_node, analyzer.sql_ast_vars)
            if affine is not None:
                has_placeholders = affine.const > 0 or bool(affine.syms)
            elif isinstance(sql, str):
                has_placeholders = _placeholder_count(sql)[0] > 0 or SENTINEL in sql
            else:
                continue
            if not has_placeholders:
                continue
            key = (name, line)
            if key in seen:
                continue
            seen.add(key)
            violations.append({"function": name, "line": line})
    return violations


def mogrify_with_params_sql(source: str) -> List[dict]:
    """Mogrified literal fragments also passed as ``execute()`` parameters.

    ``cursor.mogrify("(%s, %s, %s)", row)`` already interpolates ``row`` into
    the SQL; passing the same values again makes psycopg2 raise
    ``not all arguments converted during string formatting`` (or the reverse).
    """
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for _sql, line, sql_node, params_node, method in analyzer.execute_calls:
            if method != "execute" or params_node is None:
                continue
            if not _contains_mogrify(sql_node, analyzer.sql_ast_vars):
                continue
            parts = _fstring_parts(sql_node, analyzer.sql_ast_vars) or []
            remaining = 0
            unknown = False
            for kind, value in parts:
                if kind == "text":
                    remaining += _placeholder_count(value)[0]
                elif _contains_mogrify(value, analyzer.sql_ast_vars):
                    continue
                else:
                    affine = _sql_affine(value, analyzer.sql_ast_vars)
                    if affine is None or affine.syms:
                        unknown = True
                    else:
                        remaining += affine.const
            if not (
                remaining == 0
                or _params_include_flattening(params_node, analyzer.sql_ast_vars)
            ):
                continue
            if unknown:
                continue
            key = (name, line)
            if key in seen:
                continue
            seen.add(key)
            violations.append({"function": name, "line": line})
    return violations


def statement_placeholder_sql(source: str) -> List[dict]:
    """A SQL parameter used where a statement/subquery is required.

    Catches ``u3 AS (%s RETURNING 1)``: placeholders carry values, so the
    driver quotes the statement text and PostgreSQL raises a syntax error at
    the first keyword. The statement must be inlined into the SQL string (or
    executed separately).
    """
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql, line in analyzer.execute_sqls:
            match = _STATEMENT_PLACEHOLDER_RE.search(sql)
            if match is None:
                continue
            key = (name, line)
            if key in seen:
                continue
            seen.add(key)
            start = max(0, match.start() - 40)
            fragment = " ".join(sql[start : match.end() + 40].split())
            violations.append(
                {
                    "function": name,
                    "line": line,
                    "fragment": fragment[:160],
                }
            )
    return violations


def ambiguous_values_alias_sql(source: str) -> List[dict]:
    """``VALUES`` alias columns colliding with unqualified target columns.

    Catches ``UPDATE ORDERS ... FROM (VALUES ...) AS v(carrier_id, o_id, ...)
    WHERE O_ID = v.o_id``: ``o_id`` and ``O_ID`` are the same unqualified name,
    so PostgreSQL raises ``column reference "o_id" is ambiguous`` for every
    row. Qualifying the target column (``ORDERS.O_ID``) or renaming the alias
    column avoids the collision.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:  # pragma: no cover - declared dependency
        return []

    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql, line in analyzer.execute_sqls:
            normalized = sql.replace("%s", "0").replace("%%", "%")
            try:
                tree = sqlglot.parse_one(normalized, read="postgres")
            except Exception:
                continue
            if tree is None:
                continue
            # SET targets are not column references; only expressions can be
            # ambiguous against a FROM alias.
            set_targets = {
                id(assignment.this)
                for assignment in getattr(tree, "expressions", [])
                if isinstance(assignment, exp.EQ) and isinstance(assignment.this, exp.Column)
            }
            unqualified = {
                col.name.lower()
                for col in tree.find_all(exp.Column)
                if col.name and not col.table and id(col) not in set_targets
            }
            if not unqualified:
                continue
            for values in tree.find_all(exp.Values):
                alias_columns = values.alias_column_names
                if not alias_columns and values.parent is not None:
                    alias_columns = values.parent.alias_column_names
                if not alias_columns:
                    continue
                clashes = sorted(
                    {col.lower() for col in alias_columns} & unqualified
                )
                if not clashes:
                    continue
                key = (name, line, tuple(clashes))
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    {
                        "function": name,
                        "line": line,
                        "columns": clashes,
                    }
                )
    return violations


def _join_pattern_text(node: Optional[ast.AST]) -> Optional[str]:
    """The constant row pattern of ``",".join([pattern] * len(X))``."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
    ):
        return None
    arg = node.args[0]
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mult):
        return _join_pattern(arg.left) or _join_pattern(arg.right)
    if isinstance(arg, ast.ListComp) and len(arg.generators) == 1:
        return _const_str(arg.elt)
    return None


def _template_sql(
    node: Optional[ast.AST], sql_vars: Dict[str, ast.AST], depth: int = 0
) -> Optional[str]:
    """Reconstruct SQL with dynamic row fragments kept as their literal pattern."""
    if node is None or depth > 8:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return _template_sql(sql_vars.get(node.id), sql_vars, depth + 1)
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                value = part.value
                stored = sql_vars.get(value.id) if isinstance(value, ast.Name) else value
                pattern = _join_pattern_text(stored)
                parts.append(pattern if pattern is not None else "0")
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _template_sql(node.left, sql_vars, depth + 1)
        right = _template_sql(node.right, sql_vars, depth + 1)
        if left is None or right is None:
            return None
        return left + right
    return _join_pattern_text(node)


def table_column_prefix_mismatch_sql(source: str) -> List[dict]:
    """Single-table SQL whose column prefix is a truncated table prefix.

    Catches ``UPDATE ORDER_LINE ... WHERE O_W_ID = %s``: every other column in
    the statement is ``OL_*``, so ``O_W_ID`` is a truncation of ``OL_W_ID`` and
    the column does not exist. PostgreSQL raises UndefinedColumn for every row.
    Joined statements, subqueries, and ambiguous prefix sets are skipped.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:  # pragma: no cover - declared dependency
        return []

    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql, line, sql_node, _params, method in analyzer.execute_calls:
            if method != "execute":
                continue
            template = _template_sql(sql_node, analyzer.sql_ast_vars)
            if template is None:
                template = sql if isinstance(sql, str) else None
            if template is None:
                continue
            normalized = template.replace("%s", "0").replace("%%", "%")
            try:
                tree = sqlglot.parse_one(normalized, read="postgres")
            except Exception:
                continue
            if tree is None:
                continue
            if len(list(tree.find_all(exp.Table))) != 1:
                continue
            if list(tree.find_all(exp.Subquery)):
                continue
            columns = [col.name for col in tree.find_all(exp.Column) if col.name]
            prefixes: Dict[str, int] = {}
            for col in columns:
                if "_" in col:
                    prefix = col.split("_", 1)[0]
                    prefixes[prefix] = prefixes.get(prefix, 0) + 1
            dominant, dominant_count = ("", 0)
            if prefixes:
                dominant, dominant_count = max(prefixes.items(), key=lambda item: item[1])
            table_name = next(iter(tree.find_all(exp.Table))).name or ""
            expected = "".join(part[0] for part in table_name.split("_") if part).upper()

            for col in columns:
                if "_" not in col:
                    continue
                prefix = col.split("_", 1)[0]
                prefix_u = prefix.upper()
                expected_prefix = None
                if (
                    dominant_count >= 2
                    and prefix_u != dominant.upper()
                    and len(prefix_u) < len(dominant)
                    and dominant.upper().startswith(prefix_u)
                ):
                    expected_prefix = dominant
                elif (
                    prefix
                    and expected
                    and prefix_u != expected
                    and len(prefix_u) < len(expected)
                    and expected.startswith(prefix_u)
                ):
                    expected_prefix = expected
                if expected_prefix is None:
                    continue
                key = (name, line, col)
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    {
                        "function": name,
                        "line": line,
                        "column": col,
                        "expected_prefix": expected_prefix,
                    }
                )
    return violations


def _iter_call_sql_nodes(analyzer):
    """(sql_node, line) for execute/executemany/execute_batch/execute_values."""
    for _sql, line, sql_node, _params, method in analyzer.execute_calls:
        if method in {"execute", "executemany"}:
            yield sql_node, line
    function = analyzer.function_node
    if function is None:
        return
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
        elif isinstance(node.func, ast.Name):
            attr = node.func.id
        else:
            continue
        if attr not in {"execute_batch", "execute_values"}:
            continue
        if len(node.args) >= 3:
            yield node.args[1], getattr(node, "lineno", 0)


def _resolve_sql(analyzer, sql_node) -> Optional[str]:
    """Template SQL if reconstructable, else the resolver's best effort."""
    template = _template_sql(sql_node, analyzer.sql_ast_vars)
    if isinstance(template, str):
        return template
    resolved = analyzer.eval(sql_node)
    return resolved if isinstance(resolved, str) else None


def _single_table_own_columns(template: Optional[str]):
    """(TABLE, [columns]) for a single-table, subquery-free statement.

    Only unqualified columns (or ones qualified with the table's own name)
    are attributed to the table; alias-qualified references (``v.o_id``)
    are skipped. Returns None when the statement is not analyzable.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return None
    if not isinstance(template, str):
        return None
    normalized = template.replace("%s", "0").replace("%%", "%")
    try:
        tree = sqlglot.parse_one(normalized, read="postgres")
    except Exception:
        return None
    if tree is None or len(list(tree.find_all(exp.Table))) != 1:
        return None
    if list(tree.find_all(exp.Subquery)):
        return None
    table = next(iter(tree.find_all(exp.Table))).name or ""
    if not table:
        return None
    columns = [
        col.name.upper()
        for col in tree.find_all(exp.Column)
        if col.name and (not col.table or col.table.upper() == table.upper())
    ]
    return (table.upper(), columns)


def table_column_vocab_sql(source: str) -> Dict[str, set]:
    """Every column ever mentioned per table (single-table statements only)."""
    vocab: Dict[str, set] = {}
    for _name, analyzer in _iter_analyzed(source):
        for sql_node, _line in _iter_call_sql_nodes(analyzer):
            parsed = _single_table_own_columns(
                _resolve_sql(analyzer, sql_node)
            )
            if parsed is None:
                continue
            table, columns = parsed
            vocab.setdefault(table, set()).update(columns)
    return vocab


def table_column_sites_sql(source: str) -> List[dict]:
    """Every (function, line, table, column) reference, single-table only."""
    sites: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql_node, line in _iter_call_sql_nodes(analyzer):
            parsed = _single_table_own_columns(
                _resolve_sql(analyzer, sql_node)
            )
            if parsed is None:
                continue
            table, columns = parsed
            for column in columns:
                key = (name, line, table, column)
                if key in seen:
                    continue
                seen.add(key)
                sites.append(
                    {"function": name, "line": line, "table": table, "column": column}
                )
    return sites


def wrong_table_column_sql(original_source: str, optimized_source: str) -> List[dict]:
    """Rewrite columns that belong to a different table than the one queried.

    Catches ``... FROM ORDER_LINE WHERE ... (O_D_ID = %s AND O_ID = %s)``:
    ``O_D_ID`` never appears for ``ORDER_LINE`` anywhere in the original, but
    does appear for ``ORDERS`` — so the predicate references columns that do
    not exist on the queried table and matches nothing (or raises). Only
    flags confusion with a table the original knows; genuinely new columns
    are skipped to allow legitimate projection widening.
    """
    orig_vocab = table_column_vocab_sql(original_source)
    if not orig_vocab:
        return []
    known_elsewhere: Dict[str, set] = {}
    for table, columns in orig_vocab.items():
        for column in columns:
            known_elsewhere.setdefault(column, set()).add(table)
    violations: List[dict] = []
    seen = set()
    for site in table_column_sites_sql(optimized_source):
        table, column = site["table"], site["column"]
        if column in orig_vocab.get(table, set()):
            continue
        owners = known_elsewhere.get(column, set()) - {table}
        if not owners:
            continue
        key = (site["function"], site["line"], table, column)
        if key in seen:
            continue
        seen.add(key)
        violations.append(
            {
                "function": site["function"],
                "line": site["line"],
                "table": table,
                "column": column,
                "belongs_to": sorted(owners),
            }
        )
    return violations


def insert_column_values_arity_sql(source: str) -> List[dict]:
    """INSERT with an explicit column list whose VALUES row width differs.

    Catches ``INSERT INTO t (a, b, c) VALUES (%s, %s)``: the column count and
    the value count disagree, so the statement fails for every row. Statements
    without a column list, ``INSERT ... SELECT``, and dynamic VALUES fragments
    are skipped.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return []
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql_node, line in _iter_call_sql_nodes(analyzer):
            template = _resolve_sql(analyzer, sql_node)
            if not isinstance(template, str):
                continue
            normalized = template.replace("%s", "0").replace("%%", "%")
            try:
                tree = sqlglot.parse_one(normalized, read="postgres")
            except Exception:
                continue
            if tree is None or not isinstance(tree, exp.Insert):
                continue
            schema = tree.args.get("this")
            if not isinstance(schema, exp.Schema):
                continue
            columns = [
                c.name
                for c in schema.expressions
                if isinstance(c, (exp.Column, exp.Identifier))
            ]
            if not columns:
                continue
            values = tree.args.get("expression")
            if not isinstance(values, exp.Values):
                continue
            widths = set()
            dynamic = False
            for row in values.expressions:
                if not isinstance(row, exp.Tuple):
                    dynamic = True
                    break
                widths.add(len(row.expressions))
            if dynamic or len(widths) != 1 or next(iter(widths)) == len(columns):
                continue
            key = (name, line)
            if key in seen:
                continue
            seen.add(key)
            table = schema.this.name if isinstance(schema.this, exp.Table) else ""
            violations.append(
                {
                    "function": name,
                    "line": line,
                    "table": table.upper(),
                    "columns": len(columns),
                    "values": next(iter(widths)),
                }
            )
    return violations


def _function_reducers(function: Optional[ast.AST]) -> bool:
    """Whether the function reduces rows in Python (min/max/sum/sorted)."""
    if function is None:
        return False
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"min", "max", "sum", "sorted"}:
                return True
    return False


def aggregate_queries_sql(source: str) -> Dict[str, Dict[str, bool]]:
    """Per-function GROUP BY presence and Python-side reduction.

    Maps ``function -> {"has_group_by": bool, "has_reducer": bool}``. A query
    with GROUP BY collapses many rows into one per key (e.g. MIN(NO_O_ID) per
    district); a rewrite that fetches the raw rows must reproduce that
    reduction, either with its own GROUP BY or with min/max/sum/sorted in
    Python. Plain ``ORDER BY ... LIMIT 1`` single-row fetches are not
    aggregations and are ignored.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return {}
    result: Dict[str, Dict[str, bool]] = {}
    for name, analyzer in _iter_analyzed(source):
        function = analyzer.function_node
        if function is None:
            continue
        has_group_by = False
        for sql_node, _line in _iter_call_sql_nodes(analyzer):
            template = _resolve_sql(analyzer, sql_node)
            if not isinstance(template, str):
                continue
            normalized = template.replace("%s", "0").replace("%%", "%")
            try:
                tree = sqlglot.parse_one(normalized, read="postgres")
            except Exception:
                continue
            if tree is None:
                continue
            for select in tree.find_all(exp.Select):
                if select.args.get("group") is not None:
                    has_group_by = True
                    break
            if has_group_by:
                break
        result[name] = {
            "has_group_by": has_group_by,
            "has_reducer": _function_reducers(function),
        }
    return result


def _identity_columns(tree, exp) -> List[str]:
    """WHERE columns that address specific rows (equality + range, no ORDER BY).

    Point lookups and range-bounded fetches define a table's row identity:
    ``getNewOrder`` pins (NO_D_ID, NO_W_ID) with a NO_O_ID range, ``getCId``
    pins (O_ID, O_D_ID, O_W_ID). Scans (ORDER BY, OR, IN-lists) do not define
    identity and are skipped.
    """
    if tree.args.get("order") is not None:
        return []
    where = tree.args.get("where")
    if where is None or where.find(exp.Or) is not None or where.find(exp.In) is not None:
        return []
    where_sql = where.sql(dialect="postgres")
    return [
        match.group(1).split(".")[-1].strip('"`').lower()
        for match in re.finditer(
            r"([A-Za-z_][\w.\"]*)\s*(?:=|<>|!=|>=|<=|>|<)\s*(?:0|%s|\?|:\w+)", where_sql
        )
    ]


def orig_identity_sql(source: str) -> Dict[str, set]:
    """Row-identity columns per table, mined from the original's point queries."""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return {}
    identity: Dict[str, set] = {}
    for _name, analyzer in _iter_analyzed(source):
        for sql_node, _line in _iter_call_sql_nodes(analyzer):
            template = _resolve_sql(analyzer, sql_node)
            if not isinstance(template, str):
                continue
            normalized = template.replace("%s", "0").replace("%%", "%")
            try:
                tree = sqlglot.parse_one(normalized, read="postgres")
            except Exception:
                continue
            if tree is None or not isinstance(tree, (exp.Select, exp.Update, exp.Delete)):
                continue
            parsed = _single_table_own_columns(template)
            if parsed is None:
                continue
            table, _columns = parsed
            cols = _identity_columns(tree, exp)
            if cols:
                identity.setdefault(table, set()).update(col.upper() for col in cols)
    return identity


def _value_names(value: ast.AST) -> set:
    return {
        n.id for n in ast.walk(value)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def _contains_fetch_call(value: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"fetchone", "fetchall", "fetchmany"}
        for n in ast.walk(value)
    )


def _pinned_names(function: Optional[ast.AST]) -> set:
    """Names bound once to a txn-constant (params attribute, not fetch results).

    A name assigned exactly once from a call-free, comprehension-free RHS
    (e.g. ``w_id = params["w_id"]``) holds one value for the txn. Excluded:
    names stored inside loop bodies (``no_o_id`` rebound per district),
    anything fetch-derived (``no_o_id = new_order_map[d_id]`` flows from
    ``fetchall``), and loop/comprehension-bound names with no explicit
    assignment (``d_id``, ``row``).
    """
    if function is None:
        return set()
    in_loop: set = set()
    for node in ast.walk(function):
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            for child in list(node.body) + list(node.orelse):
                for n in ast.walk(child):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        in_loop.add(n.id)
    stores: Dict[str, int] = {}
    rhs_ok: Dict[str, bool] = {}
    values: Dict[str, ast.AST] = {}
    for node in ast.walk(function):
        targets: List[ast.AST] = []
        value: Optional[ast.AST] = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        names = [n.id for n in ast.walk(targets[0]) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)] if len(targets) == 1 else []
        clean = value is not None and not any(
            isinstance(child, (ast.Call, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.IfExp, ast.Lambda, ast.Yield, ast.YieldFrom, ast.Await))
            for child in ast.walk(value)
        )
        for name in names:
            stores[name] = stores.get(name, 0) + 1
            rhs_ok[name] = rhs_ok.get(name, True) and clean
            values[name] = value
    tainted: set = set()
    for name, value in values.items():
        if _contains_fetch_call(value):
            tainted.add(name)
    for node in ast.walk(function):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            if _value_names(node.iter) & tainted:
                for n in ast.walk(node.target):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        tainted.add(n.id)
    changed = True
    while changed:
        changed = False
        for name, value in values.items():
            if name not in tainted and _value_names(value) & tainted:
                tainted.add(name)
                changed = True
    return {
        name
        for name, count in stores.items()
        if count == 1 and rhs_ok.get(name, False)
        and name not in in_loop and name not in tainted
    }


def _key_columns(key: ast.AST, gen_target: str, select_cols: Optional[List[str]]) -> Optional[set]:
    """Table columns used as a dict key, via SELECT-list positions."""
    if select_cols is None:
        return None
    indices: List[int] = []
    if isinstance(key, ast.Subscript) and isinstance(key.value, ast.Name) and key.value.id == gen_target:
        if not isinstance(key.slice, ast.Constant) or not isinstance(key.slice.value, int):
            return None
        indices = [key.slice.value]
    elif isinstance(key, (ast.Tuple, ast.List)):
        for elt in key.elts:
            if (
                not isinstance(elt, ast.Subscript)
                or not isinstance(elt.value, ast.Name)
                or elt.value.id != gen_target
                or not isinstance(elt.slice, ast.Constant)
                or not isinstance(elt.slice.value, int)
            ):
                return None
            indices.append(elt.slice.value)
    else:
        return None
    if any(i < 0 or i >= len(select_cols) for i in indices):
        return None
    return {select_cols[i].upper() for i in indices}


def _producer_table(sql: str) -> str:
    """Uppercase table of a single-table statement, else ""."""
    parsed = _single_table_own_columns(sql)
    return parsed[0] if parsed else ""


def narrow_key_pick_sql(source: str) -> List[dict]:
    """Per-key dict picks from unordered multi-row fetches.

    Catches ``{row[0]: row[1] for row in rows}`` over a SELECT with no GROUP
    BY / ORDER BY / LIMIT and no min()/max()/sorted() in the function: the
    surviving row per key is arbitrary (dict last-wins on unordered input),
    so a later DELETE/UPDATE delivers the wrong order. Accumulations
    (``d[k] += v``, ``append``, ``Counter``, self-referential updates) combine
    every row and are safe.
    """
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        function = analyzer.function_node
        if function is None:
            continue
        if _function_reducers(function):
            continue
        executes = sorted(
            (line, sql)
            for sql, line, _node, _params, method in analyzer.execute_calls
            if isinstance(sql, str) and method == "execute"
        )
        bindings: Dict[str, set] = {}
        assigns = [
            node
            for node in ast.walk(function)
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
        ]
        assigns.sort(key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)))
        picks: List[dict] = []
        for node in assigns:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            sqls = _producer_sqls(node.value, bindings, executes, node.lineno)
            if sqls:
                for target in targets:
                    if isinstance(target, ast.Name):
                        bindings.setdefault(target.id, set()).update(sqls)
                    elif isinstance(target, (ast.Tuple, ast.List)):
                        for element in target.elts:
                            if isinstance(element, ast.Name):
                                bindings.setdefault(element.id, set()).update(sqls)
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                continue
            var = targets[0].id
            value = node.value
            if not (
                isinstance(value, ast.DictComp)
                and len(value.generators) == 1
                and not value.generators[0].ifs
                and isinstance(value.generators[0].target, ast.Name)
            ):
                continue
            gen = value.generators[0]
            if not sqls:
                continue
            keycols: Optional[set] = None
            for sql in sqls:
                cols = _key_columns(value.key, gen.target.id, find_select_columns(sql))
                if cols is not None:
                    keycols = (keycols or set()) | cols
            picks.append({"var": var, "line": node.lineno, "sqls": set(sqls), "keycols": keycols})
        for node in ast.walk(function):
            if not isinstance(node, (ast.For, ast.AsyncFor)):
                continue
            if not isinstance(node.target, ast.Name):
                continue
            loop_var = node.target.id
            iter_sqls = _producer_sqls(node.iter, bindings, executes, getattr(node, "lineno", 0))
            if not iter_sqls:
                continue
            for stmt in ast.walk(node):
                dictvar: Optional[str] = None
                key_expr: Optional[ast.AST] = None
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Subscript)
                    and isinstance(stmt.targets[0].value, ast.Name)
                ):
                    dictvar = stmt.targets[0].value.id
                    key_expr = stmt.targets[0].slice
                    if any(
                        isinstance(n, ast.Name) and n.id == dictvar
                        for n in ast.walk(stmt.value)
                    ):
                        continue
                elif (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Attribute)
                    and stmt.value.func.attr == "setdefault"
                    and isinstance(stmt.value.func.value, ast.Name)
                    and len(stmt.value.args) >= 1
                ):
                    dictvar = stmt.value.func.value.id
                    key_expr = stmt.value.args[0]
                else:
                    continue
                keycols = None
                for sql in iter_sqls:
                    cols = _key_columns(key_expr, loop_var, find_select_columns(sql))
                    if cols is not None:
                        keycols = (keycols or set()) | cols
                picks.append({"var": dictvar, "line": getattr(stmt, "lineno", 0), "sqls": set(iter_sqls), "keycols": keycols})
        for pick in picks:
            if not pick["sqls"] or pick["keycols"] is None:
                continue
            if any(
                (mods := _select_modifiers(sql)) is not None
                and (mods.get("group_by") or mods.get("order_by") or mods.get("limit"))
                for sql in pick["sqls"]
            ):
                continue
            key = (name, pick["line"], pick["var"])
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {
                    "function": name,
                    "line": pick["line"],
                    "variable": pick["var"],
                    "keycols": sorted(pick["keycols"]),
                    "sqls": sorted(pick["sqls"]),
                }
            )
    return violations


def group_by_consistency_sql(source: str) -> List[dict]:
    """Bare SELECT columns missing from the GROUP BY clause.

    Catches ``SELECT OL_D_ID, SUM(OL_AMOUNT) ... GROUP BY OL_O_ID``: every
    non-aggregated selected column must appear in GROUP BY, so PostgreSQL
    raises ``column must appear in the GROUP BY clause`` for every execution.
    Star projections and statements without GROUP BY are skipped.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:  # pragma: no cover - declared dependency
        return []

    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql_node, line in _iter_call_sql_nodes(analyzer):
            template = _resolve_sql(analyzer, sql_node)
            if not isinstance(template, str):
                continue
            if "*" in template.split("FROM", 1)[0]:
                continue
            normalized = template.replace("%s", "0").replace("%%", "%")
            try:
                tree = sqlglot.parse_one(normalized, read="postgres")
            except Exception:
                continue
            if tree is None:
                continue
            for select in tree.find_all(exp.Select):
                group = select.args.get("group")
                if group is None:
                    continue
                grouped = {
                    col.name.lower()
                    for col in group.find_all(exp.Column)
                    if col.name
                }
                for proj in select.expressions:
                    if isinstance(proj, exp.Star):
                        break
                    # Bare columns are those with no aggregate-function
                    # ancestor within this projection.
                    for col in proj.find_all(exp.Column):
                        node = col
                        under_agg = False
                        while node is not None and node is not proj:
                            node = node.parent
                            if isinstance(node, exp.AggFunc):
                                under_agg = True
                                break
                        if under_agg:
                            continue
                        if not col.name or col.name.lower() in grouped:
                            continue
                        key = (name, line, col.name.lower())
                        if key in seen:
                            continue
                        seen.add(key)
                        violations.append(
                            {
                                "function": name,
                                "line": line,
                                "column": col.name,
                            }
                        )
    return violations


def unknown_values_column_sql(source: str) -> List[dict]:
    """Qualified ``v.col`` references missing from the VALUES alias column list.

    Catches ``... FROM (VALUES ...) AS v(no_o_id, d_id, w_id) ... SET x =
    v.ol_delivery_d``: the alias declares no such column, so PostgreSQL
    raises ``column v.ol_delivery_d does not exist`` for every row. Table
    aliases and unqualified columns are ignored.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:  # pragma: no cover - declared dependency
        return []

    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql_node, line in _iter_call_sql_nodes(analyzer):
            template = _resolve_sql(analyzer, sql_node)
            if not isinstance(template, str):
                continue
            normalized = template.replace("%s", "0").replace("%%", "%")
            try:
                tree = sqlglot.parse_one(normalized, read="postgres")
            except Exception:
                continue
            if tree is None:
                continue
            aliases: Dict[str, set] = {}
            for values in tree.find_all(exp.Values):
                cols = values.alias_column_names
                if not cols:
                    continue
                try:
                    alias = str(values.alias or "").lower()
                except Exception:
                    continue
                if alias:
                    aliases.setdefault(alias, set()).update(c.lower() for c in cols)
            if not aliases:
                continue
            for col in tree.find_all(exp.Column):
                if not col.name or not col.table:
                    continue
                known = aliases.get(col.table.lower())
                if known is None or col.name.lower() in known:
                    continue
                key = (name, line, col.table.lower(), col.name.lower())
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    {
                        "function": name,
                        "line": line,
                        "alias": col.table,
                        "column": col.name,
                    }
                )
    return violations


def _normalize_column(col: str) -> str:
    return col.split(".")[-1].strip('"`').lower()


def _any_in_filter_columns(sql: str) -> set:
    """Normalized columns matched by ``col = ANY(%s)`` / ``col IN (%s)``.

    A composite LHS (a tuple before the operator) is deliberately not matched.
    """
    return {_normalize_column(m.group(1)) for m in _ANY_IN_FILTER_RE.finditer(sql)}


def fragile_composite_agg_sql(source: str) -> List[dict]:
    """Resolved SQL strings that aggregate ``ROW(...)`` with ``ARRAY_AGG``."""
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql, line in analyzer.execute_sqls:
            if SENTINEL in sql:
                continue
            if not _FRAGILE_COMPOSITE_AGG_RE.search(sql):
                continue
            key = (name, sql)
            if key in seen:
                continue
            seen.add(key)
            violations.append({"function": name, "sql": sql, "line": line})
    return violations


def composite_any_array_sql(source: str) -> List[dict]:
    """Resolved SQL comparing a composite key tuple to a single array placeholder."""
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql, line in analyzer.execute_sqls:
            if SENTINEL in sql:
                continue
            if not _COMPOSITE_ANY_ARRAY_RE.search(sql):
                continue
            key = (name, sql)
            if key in seen:
                continue
            seen.add(key)
            violations.append({"function": name, "sql": sql, "line": line})
    return violations


def lookup_key_not_selected_sql(source: str) -> List[dict]:
    """Batched lookup dicts keyed on a filter column missing from the SELECT."""
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql, line in analyzer.lookup_dicts:
            if SENTINEL in sql:
                continue
            filter_cols = _any_in_filter_columns(sql)
            if not filter_cols:
                continue
            cols = find_select_columns(sql)
            if cols is None:
                continue
            if any(col in set(cols) for col in filter_cols):
                continue
            key = (name, sql)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                {
                    "function": name,
                    "line": line,
                    "column": sorted(filter_cols)[0],
                    "sql": sql,
                }
            )
    return violations


def duplicate_column_predicate_sql(source: str) -> List[dict]:
    """Resolved SQL constraining one column by both equality and a set predicate."""
    violations: List[dict] = []
    seen = set()
    for name, analyzer in _iter_analyzed(source):
        for sql, line in analyzer.execute_sqls:
            if SENTINEL in sql:
                continue
            where_pos = _scan_keyword(sql, "WHERE", 0, 0)
            if where_pos < 0:
                continue
            clause = sql[where_pos + len("WHERE") :]
            kinds: Dict[str, set] = {}
            for predicate in _split_top_level_and(clause):
                predicate = predicate.strip().lstrip("(")
                match = _PREDICATE_RE.match(predicate)
                if not match:
                    continue
                col = _normalize_column(match.group(1))
                op = match.group(2).upper().replace(" ", "")
                if op.startswith("IN") or op.startswith("=ANY") or op.startswith("=ALL"):
                    kind = "set"
                elif op == "=":
                    kind = "eq"
                else:
                    continue
                kinds.setdefault(col, set()).add(kind)
            duplicates = sorted(col for col, k in kinds.items() if "eq" in k and "set" in k)
            if not duplicates:
                continue
            key = (name, sql, line)
            if key in seen:
                continue
            seen.add(key)
            for col in duplicates:
                violations.append(
                    {"function": name, "sql": sql, "line": line, "column": col}
                )
    return violations
