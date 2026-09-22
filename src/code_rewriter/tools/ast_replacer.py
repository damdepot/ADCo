"""AST-based function replacer for surgical code modifications."""

import ast
import textwrap
from typing import List, Optional, Tuple, Union


class _FunctionFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope_stack: List[str] = []
        self.functions: List[
            Tuple[str, str, Union[ast.FunctionDef, ast.AsyncFunctionDef]]
        ] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        qual_name = (
            ".".join(self.scope_stack + [node.name])
            if self.scope_stack
            else node.name
        )
        self.functions.append((qual_name, node.name, node))
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        qual_name = (
            ".".join(self.scope_stack + [node.name])
            if self.scope_stack
            else node.name
        )
        self.functions.append((qual_name, node.name, node))
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()


def _find_target_function(
    tree: ast.AST, target_name: str
) -> Optional[Tuple[str, str, Union[ast.FunctionDef, ast.AsyncFunctionDef]]]:
    """Locate a function node in the AST matching target_name.

    Supports qualified names (e.g., 'PostgresDriver.doDelivery') or
    unqualified names ('doDelivery').
    """
    clean_target = (
        target_name.split("::")[-1] if "::" in target_name else target_name
    )
    finder = _FunctionFinder()
    finder.visit(tree)

    # 1. Exact match on qualified name
    for item in finder.functions:
        if item[0] == clean_target:
            return item

    # 2. If target_name is unqualified (no dot), check unqualified name match across classes
    if "." not in clean_target:
        for item in finder.functions:
            if item[1] == clean_target:
                return item

    return None


def replace_function_ast(
    source_code: str,
    target_name: str,
    new_func_code: str,
    preserve_decorators: Optional[bool] = None,
) -> Tuple[bool, str, Optional[str]]:
    """Replace a specific function in Python source code using AST analysis.

    Args:
        source_code: The original source code string.
        target_name: The name of the function to replace (unqualified or qualified).
        new_func_code: The new function implementation string.
        preserve_decorators: Optional boolean controlling decorator preservation.

    Returns:
        A tuple of (success, reconstructed_source, error_message).
    """
    # Parse source code AST
    try:
        tree = ast.parse(source_code)
    except SyntaxError as e:
        return False, source_code, f"Invalid source code syntax: {e}"

    # Parse replacement function code AST
    dedented_new_code = textwrap.dedent(new_func_code).strip("\r\n")
    try:
        repl_tree = ast.parse(dedented_new_code)
    except SyntaxError as e:
        return (
            False,
            source_code,
            f"Invalid replacement code syntax or syntax error in modified code: {e}",
        )

    # Extract function node from replacement code
    repl_func_node: Optional[Union[ast.FunctionDef, ast.AsyncFunctionDef]] = None
    for n in ast.walk(repl_tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            repl_func_node = n
            break

    if repl_func_node is None:
        return (
            False,
            source_code,
            "No function definition found in replacement code",
        )

    # Locate target function in source code AST
    target = _find_target_function(tree, target_name)
    if target is None:
        return (
            False,
            source_code,
            f"Target '{target_name}' not found in source code or AST",
        )

    _, _, target_node = target

    orig_has_decorators = bool(target_node.decorator_list)
    repl_has_decorators = bool(repl_func_node.decorator_list)

    # Determine whether to preserve decorators
    if preserve_decorators is True:
        should_preserve = True
    elif preserve_decorators is False:
        should_preserve = False
    else:
        # Auto-detect: if replacement doesn't start with decorator '@', preserve existing decorators
        should_preserve = not dedented_new_code.lstrip().startswith("@")

    # Indent new code to match target function indentation
    col_offset = target_node.col_offset
    indented_new_code = textwrap.indent(dedented_new_code, " " * col_offset)

    if should_preserve and orig_has_decorators:
        start_line = target_node.lineno
        if repl_has_decorators:
            new_func_lines = indented_new_code.splitlines()[repl_func_node.lineno - 1:]
        else:
            new_func_lines = indented_new_code.splitlines()
    else:
        decorator_lines = [
            d.lineno for d in target_node.decorator_list if hasattr(d, "lineno")
        ]
        start_line = (
            min(decorator_lines + [target_node.lineno])
            if decorator_lines
            else target_node.lineno
        )
        new_func_lines = indented_new_code.splitlines()

    end_line = getattr(target_node, "end_lineno", target_node.lineno)

    # Slice and reconstruct source code lines
    source_lines = source_code.splitlines()

    reconstructed_lines = (
        source_lines[: start_line - 1]
        + new_func_lines
        + source_lines[end_line:]
    )
    reconstructed_source = "\n".join(reconstructed_lines)
    if source_code.endswith("\n"):
        reconstructed_source += "\n"

    # Validate reconstructed source syntax
    try:
        compile(reconstructed_source, "<string>", "exec")
    except Exception as e:
        return (
            False,
            source_code,
            f"Invalid replacement code syntax or syntax error in modified code: {e}",
        )

    return True, reconstructed_source, None
