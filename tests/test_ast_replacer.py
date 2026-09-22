import pytest
from src.code_rewriter.tools.ast_replacer import (
    replace_function_ast,
)


def test_replace_top_level_function():
    source = """def add(a, b):
    return a + b

def subtract(a, b):
    return a - b
"""
    new_add = """def add(a, b):
    # Optimized add
    return (a + b) * 1
"""
    success, modified, error = replace_function_ast(source, "add", new_add)
    assert success is True
    assert error is None
    assert "# Optimized add" in modified
    assert "return a - b" in modified


def test_replace_class_method_indentation():
    source = """class Calculator:
    def __init__(self):
        self.val = 0

    def compute(self, x):
        # old compute
        return x * 2
"""
    new_compute = """def compute(self, x):
    # vectorized compute
    return x ** 2
"""
    success, modified, error = replace_function_ast(source, "Calculator.compute", new_compute)
    assert success is True
    assert error is None
    assert "    def compute(self, x):" in modified
    assert "        # vectorized compute" in modified
    assert "        return x ** 2" in modified
    assert "def __init__(self):" in modified


def test_preserve_decorators():
    source = """@decorator_one
@decorator_two(arg=1)
def sensitive_function(data):
    return data.strip()
"""
    new_body = """def sensitive_function(data):
    return data.strip().lower()
"""
    # Test preserve_decorators=True
    success, modified, error = replace_function_ast(
        source, "sensitive_function", new_body, preserve_decorators=True
    )
    assert success is True
    assert "@decorator_one" in modified
    assert "@decorator_two(arg=1)" in modified
    assert "return data.strip().lower()" in modified


def test_overwrite_decorators():
    source = """@old_decorator
def guarded_func():
    return True
"""
    new_func = """@new_decorator
def guarded_func():
    return False
"""
    success, modified, error = replace_function_ast(
        source, "guarded_func", new_func, preserve_decorators=False
    )
    assert success is True
    assert "@old_decorator" not in modified
    assert "@new_decorator" in modified
    assert "return False" in modified


def test_replace_target_not_found():
    source = "def foo(): pass"
    success, modified, error = replace_function_ast(source, "bar", "def bar(): pass")
    assert success is False
    assert "Target 'bar' not found in source code or AST" in error


def test_replace_syntax_error_in_new_code():
    source = "def foo(): pass"
    broken_code = "def foo(): return 1 +"
    success, modified, error = replace_function_ast(source, "foo", broken_code)
    assert success is False
    assert "Invalid replacement code syntax or syntax error in modified code:" in error
