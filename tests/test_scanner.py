"""Tests for rewriter.tools.scanner."""

import os
import tempfile

import pytest

from src.rewriter.tools.scanner import scan, format_for_llm, FileInfo, ScanResult


def test_scan_finds_python_files():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "mypackage"))
        Path(os.path.join(tmp, "mypackage", "main.py")).write_text("print('hello')")
        Path(os.path.join(tmp, "mypackage", "utils.py")).write_text("def foo(): pass")
        Path(os.path.join(tmp, "mypackage", "README.md")).write_text("# hello")
        os.makedirs(os.path.join(tmp, "__pycache__"))
        Path(os.path.join(tmp, "__pycache__", "main.cpython-311.pyc")).write_text("")
        os.makedirs(os.path.join(tmp, ".git"))
        Path(os.path.join(tmp, ".git", "HEAD")).write_text("ref: refs/heads/main")

        result = scan(tmp)

        file_names = [f.relative_path for f in result.files]
        assert "mypackage/main.py" in file_names
        assert "mypackage/utils.py" in file_names
        assert "mypackage/README.md" in file_names
        assert not any("__pycache__" in p for p in file_names)
        assert not any(".git" in p for p in file_names)


def test_scan_excludes_sandbox():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "src"))
        Path(os.path.join(tmp, "src", "app.py")).write_text("x = 1")
        os.makedirs(os.path.join(tmp, "sandbox"))
        Path(os.path.join(tmp, "sandbox", "secret.py")).write_text("secret = 42")
        os.makedirs(os.path.join(tmp, "out"))
        Path(os.path.join(tmp, "out", "secret_output.py")).write_text("secret = 100")

        result = scan(tmp)

        file_names = [f.relative_path for f in result.files]
        assert any("app.py" in p for p in file_names)
        assert not any("sandbox" in p for p in file_names)
        assert not any("out/" in p for p in file_names)


def test_scan_produces_file_info():
    with tempfile.TemporaryDirectory() as tmp:
        content = "import os\n\ndef main():\n    print('hello')\n"
        Path(os.path.join(tmp, "app.py")).write_text(content)

        result = scan(tmp)

        assert len(result.files) == 1
        fi = result.files[0]
        assert fi.relative_path == "app.py"
        assert fi.extension == ".py"
        assert fi.size_bytes == len(content)
        assert os.path.realpath(result.root) == os.path.realpath(tmp)


def test_format_for_llm_includes_paths():
    with tempfile.TemporaryDirectory() as tmp:
        Path(os.path.join(tmp, "main.py")).write_text("import db\nconn = db.connect()\nresult = conn.execute('SELECT 1')\n")
        Path(os.path.join(tmp, "data.sql")).write_text("SELECT * FROM users;\n")

        result = scan(tmp)
        output = format_for_llm(result)

        assert "main.py" in output
        assert "data.sql" in output


def test_format_for_llm_truncates_at_max_files():
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(10):
            Path(os.path.join(tmp, f"file_{i}.py")).write_text(f"# file {i}")

        result = scan(tmp)
        output = format_for_llm(result, max_files=5)

        assert "+5 more files" in output
        assert "file_9" not in output
        assert "file_4" in output


def test_scan_result_package_name():
    result = ScanResult(root="/home/user/myapp")
    assert result.package_name() == "myapp"

    result = ScanResult(root="/home/user/myapp/")
    assert result.package_name() == "myapp"


# helper to avoid import issues in test module
from pathlib import Path as _Path


def Path(p: str):
    return _Path(p)
