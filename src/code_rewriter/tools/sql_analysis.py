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
    if isinstance(value, ast.ListComp) and len(value.generators) == 1:
        generator = value.generators[0]
        if generator.ifs:
            return None
        arity = _literal_arity(value.elt)
        symbols = _iter_symbols(generator.iter)
        if arity is None or not symbols:
            return None
        return _Affine(0, {symbols: arity})
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
