"""Shared best-effort resolver for Python expressions that yield SQL strings.

The resolver is intentionally conservative and repo-agnostic. It resolves SQL
string expressions on a best-effort basis so that multiple analyzers can share
the exact same behavior:

* module-level dict literals (including nested ``AnnAssign`` declarations)
* ``Name`` lookup, local scope first then module scope
* ``Subscript`` with a constant string key (e.g. ``q["getNewOrder"]``)
* ``%`` formatting, ``+`` and ``*`` on strings
* ``str.replace(...)`` and ``" ".join([...])``
* f-strings (non-constant fields are replaced with ``"0"``)

When a value cannot be resolved the functions return ``None``. When a
resolution is only partial, string concatenation/multiplication and joins may
return :data:`SENTINEL` to signal an unknown fragment.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Optional

SENTINEL = "\u0000"
_FORMAT_SPEC_RE = re.compile(r"%(?:\d+)?[sdfr]")


def _const_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _const_int(node: ast.AST) -> Optional[int]:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    return None


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


def collect_module_dicts(tree: ast.Module) -> Dict[str, Any]:
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


def resolve(node: Optional[ast.AST], local_vars: Dict[str, Any], module_dicts: Dict[str, Any]) -> Any:
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
        base = resolve(node.value, local_vars, module_dicts)
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
        return [resolve(elt, local_vars, module_dicts) for elt in node.elts]
    return None


def _eval_binop(node: ast.BinOp, local_vars: Dict[str, Any], module_dicts: Dict[str, Any]) -> Any:
    if isinstance(node.op, ast.Mod):
        fmt = resolve(node.left, local_vars, module_dicts)
        if not isinstance(fmt, str):
            return None
        count = len(_FORMAT_SPEC_RE.findall(fmt.replace("%%", "")))
        try:
            return fmt % ((0,) * count)
        except (TypeError, ValueError):
            return None
    if isinstance(node.op, ast.Add):
        left = resolve(node.left, local_vars, module_dicts)
        right = resolve(node.right, local_vars, module_dicts)
        if left is None and right is None:
            return None
        if left is not None and not isinstance(left, str):
            return None
        if right is not None and not isinstance(right, str):
            return None
        return (left if isinstance(left, str) else SENTINEL) + (right if isinstance(right, str) else SENTINEL)
    if isinstance(node.op, ast.Mult):
        left = resolve(node.left, local_vars, module_dicts)
        right = resolve(node.right, local_vars, module_dicts)
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
            return SENTINEL
        return None
    return None


def _eval_call(node: ast.Call, local_vars: Dict[str, Any], module_dicts: Dict[str, Any]) -> Any:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "replace" and len(node.args) >= 2:
        receiver = resolve(func.value, local_vars, module_dicts)
        needle = resolve(node.args[0], local_vars, module_dicts)
        replacement = resolve(node.args[1], local_vars, module_dicts)
        if not isinstance(receiver, str) or not isinstance(needle, str):
            return None
        repl = replacement if isinstance(replacement, str) else SENTINEL
        try:
            return receiver.replace(needle, repl)
        except Exception:
            return None
    if isinstance(func, ast.Attribute) and func.attr == "join" and len(node.args) == 1:
        separator = resolve(func.value, local_vars, module_dicts)
        if not isinstance(separator, str):
            return None
        seq = node.args[0]
        if isinstance(seq, (ast.List, ast.Tuple, ast.Set)):
            parts: List[str] = []
            for elt in seq.elts:
                resolved = resolve(elt, local_vars, module_dicts)
                if not isinstance(resolved, str):
                    return SENTINEL
                parts.append(resolved)
            return separator.join(parts)
        return SENTINEL
    if isinstance(func, ast.Name) and func.id == "len":
        return SENTINEL
    return None


def _eval_joined_str(node: ast.JoinedStr) -> str:
    parts: List[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            parts.append("0")
    return "".join(parts)
