"""Format string validator for Python code using AST analysis.

Validates % formatting and str.format() calls against string templates,
detecting argument count mismatches, missing dict keys, and unescaped
DB-API placeholders.
"""

import ast
import re
from typing import Any

HINT_DB_API = (
    "HINT: In database queries undergoing Python '%' formatting, "
    "DB-API parameter placeholders must be escaped as '%%s' "
    "so Python string formatting does not consume them."
)

PERCENT_SPECIFIER_RE = re.compile(
    r"%(?:"
    r"(?P<escaped>%)"
    r"|"
    r"(?:\((?P<key>[^)]+)\))?"
    r"(?P<flags>[-+ #0]*)"
    r"(?P<width>\*|\d+)?"
    r"(?P<prec>\.(?:\*|\d+))?"
    r"(?P<len>[hlL])?"
    r"(?P<type>[diouxXeEfFgGcrsa])"
    r")"
)


class Scope:
    """Simple hierarchical symbol table for string and dictionary constants."""

    def __init__(self, parent: "Scope | None" = None) -> None:
        self.parent = parent
        self.bindings: dict[str, Any] = {}

    def get(self, name: str) -> Any:
        if name in self.bindings:
            return self.bindings[name]
        if self.parent is not None:
            return self.parent.get(name)
        return None

    def set(self, name: str, value: Any) -> None:
        self.bindings[name] = value


def resolve_value(node: ast.AST | None, scope: Scope) -> Any:
    """Attempt to evaluate an AST node to a constant value, string, or dictionary."""
    if node is None:
        return None

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        return scope.get(node.id)

    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            left = resolve_value(node.left, scope)
            right = resolve_value(node.right, scope)
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            if isinstance(left, bytes) and isinstance(right, bytes):
                return left + right
        return None

    if isinstance(node, ast.Dict):
        res: dict[Any, Any] = {}
        for k, v in zip(node.keys, node.values):
            if k is None:
                unpacked = resolve_value(v, scope)
                if isinstance(unpacked, dict):
                    res.update(unpacked)
            else:
                k_val = resolve_value(k, scope)
                v_val = resolve_value(v, scope)
                if k_val is not None:
                    res[k_val] = v_val
        return res

    if isinstance(node, ast.Subscript):
        target = resolve_value(node.value, scope)
        slice_val = resolve_value(node.slice, scope)
        if isinstance(target, dict) and slice_val in target:
            return target[slice_val]
        return None

    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
            attr_val = scope.get(node.attr)
            if attr_val is not None:
                return attr_val
        val = resolve_value(node.value, scope)
        if isinstance(val, dict) and node.attr in val:
            return val[node.attr]
        return None

    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            target = resolve_value(node.func.value, scope)
            if isinstance(target, dict) and node.args:
                key = resolve_value(node.args[0], scope)
                if key in target:
                    return target[key]
                if len(node.args) > 1:
                    return resolve_value(node.args[1], scope)
        return None

    return None


def parse_percent_specifiers(fmt_str: str) -> tuple[list[dict[str, Any]], set[str]]:
    """Parse % format specifiers from fmt_str.

    Returns:
        (positional_specs, named_keys)
    """
    positional_specs: list[dict[str, Any]] = []
    named_keys: set[str] = set()
    for m in PERCENT_SPECIFIER_RE.finditer(fmt_str):
        if m.group("escaped") == "%":
            continue
        key = m.group("key")
        if key is not None:
            named_keys.add(key)
        else:
            width = m.group("width")
            prec = m.group("prec")
            count = 1
            if width == "*":
                count += 1
            if prec == ".*":
                count += 1
            positional_specs.append({
                "type": m.group("type"),
                "count": count,
                "raw": m.group(0),
            })
    return positional_specs, named_keys


class FormatStringValidator(ast.NodeVisitor):
    def __init__(self, filename: str = "") -> None:
        self.filename = filename
        self.errors: list[str] = []
        self.root_scope = Scope()
        self.current_scope = self.root_scope

    def _line_prefix(self, node: ast.AST) -> str:
        lineno = getattr(node, "lineno", 0)
        return f"Line {lineno}"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        old_scope = self.current_scope
        self.current_scope = Scope(parent=old_scope)
        self.generic_visit(node)
        self.current_scope = old_scope

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        old_scope = self.current_scope
        self.current_scope = Scope(parent=old_scope)
        self.generic_visit(node)
        self.current_scope = old_scope

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        old_scope = self.current_scope
        self.current_scope = Scope(parent=old_scope)
        self.generic_visit(node)
        self.current_scope = old_scope

    def visit_Assign(self, node: ast.Assign) -> None:
        val = resolve_value(node.value, self.current_scope)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.current_scope.set(target.id, val)
            elif isinstance(target, ast.Subscript):
                target_dict = resolve_value(target.value, self.current_scope)
                target_key = resolve_value(target.slice, self.current_scope)
                if isinstance(target_dict, dict) and target_key is not None:
                    target_dict[target_key] = val
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            val = resolve_value(node.value, self.current_scope)
            if isinstance(node.target, ast.Name):
                self.current_scope.set(node.target.id, val)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name) and isinstance(node.op, ast.Add):
            existing = self.current_scope.get(node.target.id)
            if isinstance(existing, str):
                add_val = resolve_value(node.value, self.current_scope)
                if isinstance(add_val, str):
                    self.current_scope.set(node.target.id, existing + add_val)
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Mod):
            fmt_str = resolve_value(node.left, self.current_scope)
            if isinstance(fmt_str, str):
                self._validate_percent_format(node, fmt_str)
        self.generic_visit(node)

    def _validate_percent_format(self, node: ast.BinOp, fmt_str: str) -> None:
        positional_specs, named_keys = parse_percent_specifiers(fmt_str)
        line = self._line_prefix(node)

        if named_keys and positional_specs:
            self.errors.append(
                f"{line}: Format string cannot mix positional and named "
                f"format specifiers (format: {fmt_str!r})."
            )
            return

        if named_keys:
            if isinstance(node.right, ast.Dict):
                provided_keys: set[str] = set()
                for k in node.right.keys:
                    if k is not None:
                        k_val = resolve_value(k, self.current_scope)
                        if isinstance(k_val, str):
                            provided_keys.add(k_val)
                missing = named_keys - provided_keys
                if missing:
                    self.errors.append(
                        f"{line}: Format string requires key(s) {sorted(missing)}, "
                        f"but dictionary is missing them (format: {fmt_str!r})."
                    )
            elif isinstance(node.right, ast.Tuple):
                self.errors.append(
                    f"{line}: Format string requires a mapping/dict for named "
                    f"format specifiers, got tuple (format: {fmt_str!r})."
                )
            return

        expected_count = sum(s["count"] for s in positional_specs)
        actual_count: int

        if isinstance(node.right, ast.Tuple):
            actual_count = len(node.right.elts)
        elif isinstance(node.right, ast.Dict):
            actual_count = 1
        else:
            actual_count = 1

        if actual_count != expected_count:
            msg = (
                f"{line}: Format string requires {expected_count} argument(s), "
                f"but {actual_count} were provided (format: {fmt_str!r})."
            )
            has_unescaped_s = any(s["type"] == "s" for s in positional_specs)
            has_other_specs = any(s["type"] != "s" for s in positional_specs) or len(positional_specs) > 1
            if has_unescaped_s and (has_other_specs or actual_count < expected_count):
                if actual_count < expected_count:
                    msg += f" {HINT_DB_API}"
            self.errors.append(msg)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            fmt_str = resolve_value(node.func.value, self.current_scope)
            if isinstance(fmt_str, str):
                self._validate_str_format(node, fmt_str)
        self.generic_visit(node)

    def _validate_str_format(self, node: ast.Call, fmt_str: str) -> None:
        cleaned = fmt_str.replace("{{", "").replace("}}", "")
        placeholders = re.findall(r"\{([^{}]*)\}", cleaned)

        auto_count = 0
        manual_indices: list[int] = []

        for p in placeholders:
            field = p.split(":")[0].split("!")[0].strip()
            if field == "":
                auto_count += 1
            elif field.isdigit():
                manual_indices.append(int(field))
            else:
                m = re.match(r"^(\d+)", field)
                if m:
                    manual_indices.append(int(m.group(1)))

        needed_args = 0
        if auto_count > 0:
            needed_args = max(needed_args, auto_count)
        if manual_indices:
            needed_args = max(needed_args, max(manual_indices) + 1)

        provided_args = len(node.args)
        has_starred = any(isinstance(a, ast.Starred) for a in node.args)

        if not has_starred and provided_args < needed_args:
            line = self._line_prefix(node)
            self.errors.append(
                f"{line}: str.format() expects at least {needed_args} positional argument(s), "
                f"but only {provided_args} provided (format: {fmt_str!r})."
            )


def validate_format_strings(code: str, filename: str = "") -> list[str]:
    """Validate format strings (% and str.format()) in Python code using AST analysis.

    Returns a list of error descriptions, or an empty list if valid.
    Catches SyntaxError gracefully and returns [].
    """
    try:
        tree = ast.parse(code, filename=filename or "<string>")
    except SyntaxError:
        return []

    validator = FormatStringValidator(filename=filename)

    # Pre-populate root scope with top-level assignments
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            val = resolve_value(stmt.value, validator.root_scope)
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    validator.root_scope.set(target.id, val)
                elif isinstance(target, ast.Subscript):
                    target_dict = resolve_value(target.value, validator.root_scope)
                    target_key = resolve_value(target.slice, validator.root_scope)
                    if isinstance(target_dict, dict) and target_key is not None:
                        target_dict[target_key] = val
        elif isinstance(stmt, ast.ClassDef):
            for class_stmt in stmt.body:
                if isinstance(class_stmt, ast.Assign):
                    val = resolve_value(class_stmt.value, validator.root_scope)
                    for target in class_stmt.targets:
                        if isinstance(target, ast.Name):
                            validator.root_scope.set(target.id, val)

    validator.visit(tree)
    return validator.errors
