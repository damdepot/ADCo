"""Generic per-function database interaction modeling.

This module builds a schema-agnostic model of how a Python function talks to a
database.  It combines two sources of evidence:

* the Python AST (which statements execute SQL, in what order, and which
  results flow into later parameters), and
* the resolved SQL text (parsed with :mod:`sqlglot` to recover structural
  properties such as relations, joins, aggregates and placeholders).

Nothing here is specific to a particular schema, table or column: the model is
derived entirely from the shape of the code and the shape of the SQL.

The public API is intentionally small:

``normalize_placeholders``
    Rewrite driver-specific placeholders to a neutral ``?``.
``analyze_sql``
    Parse a resolved SQL string into a :class:`SqlModel`.
``build_function_model``
    Walk one function AST and produce a :class:`FunctionDbModel`.
``build_read_write_map``
    Aggregate per-function models into a ``table -> readers/writers`` map.
"""

from __future__ import annotations

import ast
from typing import Any, Dict, Iterable, List, Optional, Tuple

import sqlglot
from sqlglot import exp

from ..models.ast_models import FileAnalysis
from ..models.db_interaction_models import (
    FunctionDbModel,
    SqlModel,
    StatementModel,
)
from .sql_resolver import resolve

_FETCH_METHODS = {"fetchone", "fetchall", "fetchmany"}
_EXECUTE_METHODS = {"execute", "executemany"}
_COMMIT_METHODS = {"commit", "rollback"}
_SQL_VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE")

#: Dialects retried, in order, when the default dialect fails to parse a query.
#: ``None`` means sqlglot's default (most permissive) dialect.
_DIALECTS: Tuple[Optional[str], ...] = (
    None,
    "postgres",
    "mysql",
    "sqlite",
    "duckdb",
    "tsql",
    "redshift",
    "snowflake",
    "bigquery",
)


def normalize_placeholders(sql: str) -> str:
    """Replace driver placeholders with a neutral ``?`` so sqlglot can parse.

    Handles ``%s`` (psycopg/MySQL), ``?`` (sqlite/DB-API), ``$1..$n`` (psycopg3)
    and ``:name`` (SQLAlchemy/OCI).  The input is expected to be *already*
    resolved/``%``-formatted SQL, so this function only rewrites placeholder
    tokens; it never performs any ``%`` formatting itself.

    The scan is quote-aware for ``'``, ``"`` and backtick literals so that
    placeholder-looking text inside string literals is left untouched.  ``::``
    casts are preserved.  This is a best-effort normalizer, not a full SQL
    lexer: dollar-quoted strings and nested comments are not modelled.
    """
    if not isinstance(sql, str) or not sql:
        return sql

    out: List[str] = []
    i = 0
    n = len(sql)
    quote: Optional[str] = None
    while i < n:
        c = sql[i]
        if quote is not None:
            out.append(c)
            if c == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if c in ("'", '"', "`"):
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "%" and i + 1 < n and sql[i + 1] == "s":
            out.append("?")
            i += 2
            continue
        if c == "$" and i + 1 < n and sql[i + 1].isdigit():
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            out.append("?")
            i = j
            continue
        if c == ":" and not (i > 0 and sql[i - 1] == ":") and i + 1 < n and (
            sql[i + 1].isalpha() or sql[i + 1] == "_"
        ):
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            out.append("?")
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _table_names(expr: exp.Expression) -> List[str]:
    seen = set()
    names: List[str] = []
    for table in expr.find_all(exp.Table):
        name = table.name
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _dml_target_names(expr: exp.Expression) -> List[str]:
    target = expr.args.get("this")
    if isinstance(target, exp.Table):
        return [target.name] if target.name else []
    if isinstance(target, exp.Schema):
        inner = target.this
        if isinstance(inner, exp.Table) and inner.name:
            return [inner.name]
    return []


def _collect_without_subqueries(
    node: exp.Expression, typ: type, out: List[exp.Expression]
) -> None:
    """Collect ``typ`` descendants but do not descend into nested SELECTs."""
    for child in node.iter_expressions():
        if isinstance(child, exp.Select):
            continue
        if isinstance(child, typ):
            out.append(child)
        _collect_without_subqueries(child, typ, out)


def _aggregate_name(agg: exp.Expression) -> str:
    name = getattr(agg, "sql_name", None)
    if callable(name):
        try:
            value = name()
            if value:
                return str(value).upper()
        except Exception:
            pass
    return type(agg).__name__.upper()


def analyze_sql(sql: str) -> Optional[SqlModel]:
    """Parse *sql* and return its structural :class:`SqlModel`.

    Returns ``None`` when the statement cannot be parsed by any attempted
    dialect, or when the input is not a usable SQL string.
    """
    if not isinstance(sql, str) or not sql.strip():
        return None

    normalized = normalize_placeholders(sql)
    parsed: Optional[exp.Expression] = None
    for dialect in _DIALECTS:
        try:
            parsed = sqlglot.parse_one(normalized, read=dialect)
            break
        except Exception:
            parsed = None
    if parsed is None:
        return None

    tables_read: List[str]
    tables_written: List[str]
    if isinstance(parsed, (exp.Insert, exp.Update, exp.Delete)):
        tables_written = _dml_target_names(parsed)
        written_upper = {name.upper() for name in tables_written}
        tables_read = [t for t in _table_names(parsed) if t.upper() not in written_upper]
    else:
        tables_read = _table_names(parsed)
        tables_written = []

    top_level_relations = 0
    join_count = 0
    aggregate_funcs: List[str] = []
    if isinstance(parsed, exp.Select):
        from_node = parsed.args.get("from_")
        if from_node is not None and from_node.this is not None:
            top_level_relations += 1
        joins = parsed.args.get("joins") or []
        join_count = len(joins)
        top_level_relations += join_count

        found: List[exp.Expression] = []
        _collect_without_subqueries(parsed, exp.AggFunc, found)
        for agg in found:
            name = _aggregate_name(agg)
            if name not in aggregate_funcs:
                aggregate_funcs.append(name)

    return SqlModel(
        tables_read=tables_read,
        tables_written=tables_written,
        top_level_relations=top_level_relations,
        join_count=join_count,
        has_aggregate=bool(aggregate_funcs),
        aggregate_funcs=aggregate_funcs,
        has_distinct=parsed.find(exp.Distinct) is not None,
        has_subquery=parsed.find(exp.Subquery) is not None,
        placeholder_count=normalized.count("?"),
        parse_ok=True,
    )


def _call_attr(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _target_names(target: ast.AST) -> List[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: List[str] = []
        for elt in target.elts:
            names.extend(_target_names(elt))
        return names
    return []


def _param_names(arg: ast.AST) -> List[str]:
    names: List[str] = []
    if isinstance(arg, ast.Name):
        names.append(arg.id)
    elif isinstance(arg, (ast.Tuple, ast.List, ast.Set)):
        for elt in arg.elts:
            if isinstance(elt, ast.Name):
                names.append(elt.id)
    return names


def _sql_operation(sql: str) -> str:
    upper = sql.strip().upper()
    for verb in _SQL_VERBS:
        if upper.startswith(verb):
            return verb
    return "OTHER"


class _ModelBuilder:
    """Stateful walker that turns one function body into a FunctionDbModel."""

    def __init__(self, module_dicts: Dict[str, Any], file: str, function: str) -> None:
        self.module_dicts = module_dicts
        self.file = file
        self.function = function
        self.local_env: Dict[str, Any] = {}
        self.derived_from: Dict[str, int] = {}
        self.statements: List[StatementModel] = []
        self.value_edges: List[Tuple[int, int]] = []
        self.commit_positions: List[int] = []
        self.loop_depth = 0

    # -- helpers ---------------------------------------------------------
    def _last_statement_index(self) -> Optional[int]:
        return len(self.statements) - 1 if self.statements else None

    def _producer_for_value(self, value: ast.AST) -> Optional[int]:
        if _call_attr(value) in _FETCH_METHODS:
            return self._last_statement_index()
        if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name):
            return self.derived_from.get(value.value.id)
        if isinstance(value, ast.Name):
            return self.derived_from.get(value.id)
        return None

    # -- statement dispatch ---------------------------------------------
    def build(self, fn_node: ast.AST) -> None:
        self._process_body(getattr(fn_node, "body", []) or [])

    def _process_body(self, body: Iterable[ast.stmt]) -> None:
        for stmt in body:
            self._process_stmt(stmt)

    def _process_stmt(self, stmt: ast.AST) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(stmt, ast.Assign):
            self._process_assign(stmt)
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None:
                self._process_assign_like([stmt.target], stmt.value)
        elif isinstance(stmt, ast.Expr):
            self._process_expr_call(stmt.value)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            self._process_for(stmt)
        elif isinstance(stmt, ast.While):
            self.loop_depth += 1
            self._process_body(stmt.body)
            self.loop_depth -= 1
            self._process_body(stmt.orelse)
        elif isinstance(stmt, ast.If):
            self._process_body(stmt.body)
            self._process_body(stmt.orelse)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            self._process_body(stmt.body)
        elif isinstance(stmt, ast.Try):
            self._process_body(stmt.body)
            for handler in stmt.handlers:
                self._process_body(handler.body)
            self._process_body(stmt.orelse)
            self._process_body(stmt.finalbody)

    def _process_for(self, stmt: Any) -> None:
        producer = self._producer_for_value(stmt.iter)
        if producer is not None:
            for name in _target_names(stmt.target):
                self.derived_from[name] = producer
        self.loop_depth += 1
        self._process_body(stmt.body)
        self.loop_depth -= 1
        self._process_body(stmt.orelse)

    def _process_assign(self, stmt: ast.Assign) -> None:
        self._process_assign_like(stmt.targets, stmt.value)

    def _process_assign_like(self, targets: List[ast.AST], value: ast.AST) -> None:
        resolved = resolve(value, self.local_env, self.module_dicts)
        producer = self._producer_for_value(value)
        for target in targets:
            if isinstance(target, ast.Name):
                self.local_env[target.id] = resolved
                if producer is not None:
                    self.derived_from[target.id] = producer
            elif isinstance(target, (ast.Tuple, ast.List)):
                for elt in target.elts:
                    if isinstance(elt, ast.Name) and producer is not None:
                        self.derived_from[elt.id] = producer

    def _process_expr_call(self, node: ast.AST) -> None:
        if not isinstance(node, ast.Call):
            return
        attr = _call_attr(node)
        if attr in _EXECUTE_METHODS:
            self._record_execute(node)
        elif attr in _COMMIT_METHODS:
            self._record_commit()

    def _record_execute(self, call: ast.Call) -> None:
        if not call.args:
            return
        sql = resolve(call.args[0], self.local_env, self.module_dicts)
        if not isinstance(sql, str):
            return
        index = len(self.statements)
        self.statements.append(
            StatementModel(
                index=index,
                sql=sql,
                sql_operation=_sql_operation(sql),
                source_line=getattr(call, "lineno", -1),
                model=analyze_sql(sql),
            )
        )
        # Loop bodies are iteration-scoped: statements inside a loop are
        # re-executed per iteration and are modelled as an independent block,
        # so no function-level value edge is emitted for them.
        if self.loop_depth == 0 and len(call.args) > 1:
            for name in _param_names(call.args[1]):
                producer = self.derived_from.get(name)
                if producer is None:
                    continue
                edge = (producer, index)
                if edge not in self.value_edges:
                    self.value_edges.append(edge)

    def _record_commit(self) -> None:
        index = self._last_statement_index()
        if index is not None:
            self.commit_positions.append(index)


def build_function_model(
    fn_node: ast.AST,
    module_dicts: dict,
    file: str = "",
    function: str = "",
) -> FunctionDbModel:
    """Build a :class:`FunctionDbModel` for a single function AST node.

    ``module_dicts`` should come from
    :func:`code_rewriter.tools.sql_resolver.collect_module_dicts`.  The builder
    is defensive: malformed or unusual ASTs never raise, they simply produce a
    model from whatever could be resolved.
    """
    name = function or getattr(fn_node, "name", "") or ""
    builder = _ModelBuilder(module_dicts or {}, file or "", name)
    try:
        builder.build(fn_node)
    except Exception:
        pass
    return FunctionDbModel(
        function=name,
        file=file or "",
        statements=builder.statements,
        value_edges=builder.value_edges,
        commit_positions=builder.commit_positions,
    )


def build_read_write_map(
    analyses: "Dict[str, FileAnalysis] | FileAnalysis",
) -> Dict[str, Dict[str, List[str]]]:
    """Aggregate resolved SQL into a ``table -> {read_by, written_by}`` map.

    Accepts either a mapping of file path to :class:`FileAnalysis` or a single
    :class:`FileAnalysis`.  Functions are identified by their qualified name.
    Reader/writer lists are de-duplicated and sorted.
    """
    if isinstance(analyses, FileAnalysis):
        files: Iterable[FileAnalysis] = [analyses]
    else:
        files = list(analyses.values())

    result: Dict[str, Dict[str, List[str]]] = {}
    for analysis in files:
        if analysis is None:
            continue
        functions = list(analysis.functions)
        for cls in analysis.classes:
            functions.extend(cls.methods)
        for fn in functions:
            qualified = fn.qualified_name or fn.name
            for op in fn.database_operations:
                if not op.sql:
                    continue
                model = analyze_sql(op.sql)
                if model is None:
                    continue
                for table in model.tables_read:
                    entry = result.setdefault(table, {"read_by": [], "written_by": []})
                    if qualified not in entry["read_by"]:
                        entry["read_by"].append(qualified)
                for table in model.tables_written:
                    entry = result.setdefault(table, {"read_by": [], "written_by": []})
                    if qualified not in entry["written_by"]:
                        entry["written_by"].append(qualified)

    for entry in result.values():
        entry["read_by"].sort()
        entry["written_by"].sort()
    return result
