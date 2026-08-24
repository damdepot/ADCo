"""Tests for rewriter.tools.copier."""

import os
import tempfile
from pathlib import Path

import pytest

import src.rewriter.tools.copier as copier_mod
from src.rewriter.tools.copier import copy_entire, rewrite_imports, read_files


@pytest.fixture
def temp_sandbox_root(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(copier_mod, "SANDBOX_ROOT", tmp)
    yield tmp
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def test_copy_entire_copies_files_to_sandbox(temp_sandbox_root):
    with tempfile.TemporaryDirectory() as source:
        Path(os.path.join(source, "app.py")).write_text("print('hello')")
        Path(os.path.join(source, "db.py")).write_text("import sqlite3")
        os.makedirs(os.path.join(source, "lib"))
        Path(os.path.join(source, "lib", "helpers.py")).write_text("def foo(): pass")

        dest = copy_entire(source, sandbox_id="test_copier_1")

        assert os.path.isdir(dest)
        assert os.path.isfile(os.path.join(dest, "app.py"))
        assert os.path.isfile(os.path.join(dest, "db.py"))
        assert os.path.isfile(os.path.join(dest, "lib", "helpers.py"))


def test_copy_entire_dest_override(temp_sandbox_root):
    with tempfile.TemporaryDirectory() as source:
        Path(os.path.join(source, "app.py")).write_text("print('hello')")
        with tempfile.TemporaryDirectory() as dest_override:
            custom_dest = os.path.join(dest_override, "my_custom_sandbox")
            dest = copy_entire(source, dest_override=custom_dest)
            assert dest == os.path.abspath(custom_dest)
            assert os.path.isdir(dest)
            assert os.path.isfile(os.path.join(dest, "app.py"))


def test_copy_entire_excludes_pycache_and_git(temp_sandbox_root):
    with tempfile.TemporaryDirectory() as source:
        Path(os.path.join(source, "main.py")).write_text("x=1")
        os.makedirs(os.path.join(source, "__pycache__"))
        Path(os.path.join(source, "__pycache__", "main.cpython-311.pyc")).write_text("")
        os.makedirs(os.path.join(source, ".git"))
        Path(os.path.join(source, ".git", "config")).write_text("")
        os.makedirs(os.path.join(source, "sandbox"))
        Path(os.path.join(source, "sandbox", "secret.py")).write_text("secret")

        dest = copy_entire(source, sandbox_id="test_copier_exclude")

        assert os.path.isfile(os.path.join(dest, "main.py"))
        assert not os.path.exists(os.path.join(dest, "__pycache__"))
        assert not os.path.exists(os.path.join(dest, ".git"))
        assert not os.path.exists(os.path.join(dest, "sandbox"))


def test_rewrite_imports_from_pkg_sub_util(temp_sandbox_root):
    with tempfile.TemporaryDirectory() as project_root:
        project_root_path = Path(project_root)
        (project_root_path / "pyproject.toml").write_text("[project]\nname='test'")
        (project_root_path / "pkg").mkdir()
        (project_root_path / "pkg" / "sub").mkdir()
        (project_root_path / "pkg" / "sub" / "__init__.py").write_text("")

        source_root = str(project_root_path / "pkg" / "sub")
        dest = str(project_root_path / "sandbox")

        shutil = __import__("shutil")
        shutil.copytree(source_root, dest)

        test_file = os.path.join(dest, "test_file.py")
        Path(test_file).write_text("from pkg.sub.util import X\n")

        count = rewrite_imports(dest, source_root)
        assert count >= 1

        rewritten = Path(test_file).read_text()
        assert "from util import X" in rewritten
        assert "pkg.sub" not in rewritten


def test_rewrite_imports_import_pkg_sub_worker_as_worker(temp_sandbox_root):
    with tempfile.TemporaryDirectory() as project_root:
        project_root_path = Path(project_root)
        (project_root_path / "pyproject.toml").write_text("[project]\nname='test'")
        (project_root_path / "pkg").mkdir()
        (project_root_path / "pkg" / "sub").mkdir()
        (project_root_path / "pkg" / "sub" / "__init__.py").write_text("")

        source_root = str(project_root_path / "pkg" / "sub")
        dest = str(project_root_path / "sandbox")

        shutil = __import__("shutil")
        shutil.copytree(source_root, dest)

        test_file = os.path.join(dest, "worker_test.py")
        Path(test_file).write_text("import pkg.sub.worker as worker\n")

        count = rewrite_imports(dest, source_root)
        rewritten = Path(test_file).read_text()
        assert "import worker as worker" in rewritten
        assert "pkg.sub.worker" not in rewritten


def test_rewrite_imports_dunder_import(temp_sandbox_root):
    with tempfile.TemporaryDirectory() as project_root:
        project_root_path = Path(project_root)
        (project_root_path / "pyproject.toml").write_text("[project]\nname='test'")
        (project_root_path / "pkg").mkdir()
        (project_root_path / "pkg" / "sub").mkdir()
        (project_root_path / "pkg" / "sub" / "__init__.py").write_text("")

        source_root = str(project_root_path / "pkg" / "sub")
        dest = str(project_root_path / "sandbox")

        shutil = __import__("shutil")
        shutil.copytree(source_root, dest)

        test_file = os.path.join(dest, "dynamic_test.py")
        Path(test_file).write_text("__import__('pkg.sub.drivers.X')\n")

        count = rewrite_imports(dest, source_root)
        rewritten = Path(test_file).read_text()
        assert "__import__('drivers.X')" in rewritten
        assert "pkg.sub.drivers" not in rewritten


def test_rewrite_imports_from_pkg_sub_import_constants(temp_sandbox_root):
    with tempfile.TemporaryDirectory() as project_root:
        project_root_path = Path(project_root)
        (project_root_path / "pyproject.toml").write_text("[project]\nname='test'")
        (project_root_path / "pkg").mkdir()
        (project_root_path / "pkg" / "sub").mkdir()
        (project_root_path / "pkg" / "sub" / "__init__.py").write_text("")
        (project_root_path / "pkg" / "sub" / "constants.py").write_text("X=1")

        source_root = str(project_root_path / "pkg" / "sub")
        dest = str(project_root_path / "sandbox")

        shutil = __import__("shutil")
        shutil.copytree(source_root, dest)

        test_file = os.path.join(dest, "constants_test.py")
        Path(test_file).write_text("from pkg.sub import constants\n")

        count = rewrite_imports(dest, source_root)
        rewritten = Path(test_file).read_text()
        assert "import constants" in rewritten
        assert "from pkg.sub import" not in rewritten


def test_read_files_reads_contents(temp_sandbox_root):
    with tempfile.TemporaryDirectory() as root:
        Path(os.path.join(root, "a.py")).write_text("a_content")
        Path(os.path.join(root, "b.txt")).write_text("b_content")

        result = read_files(root, ["a.py", "b.txt", "missing.py"])

        assert result["a.py"] == "a_content"
        assert result["b.txt"] == "b_content"
        assert "missing.py" not in result


def test_read_files_skips_errors(temp_sandbox_root):
    with tempfile.TemporaryDirectory() as root:
        result = read_files(root, ["nonexistent.py"])
        assert result == {}
