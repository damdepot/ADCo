"""Unit tests for file_tools module."""

from pathlib import Path
import pytest
from src.knob_tuner.tools.file_tools import read_json_file, write_json_file


def test_read_json_file_dict(tmp_path: Path):
    file_path = tmp_path / "test.json"
    file_path.write_text('{"key": "value", "numbers": [1, 2, 3]}', encoding="utf-8")

    data = read_json_file(str(file_path))
    assert data == {"key": "value", "numbers": [1, 2, 3]}


def test_read_json_file_list(tmp_path: Path):
    file_path = tmp_path / "list.json"
    file_path.write_text('[{"knob": "shared_buffers", "value": "256MB"}]', encoding="utf-8")

    data = read_json_file(str(file_path))
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["knob"] == "shared_buffers"


def test_read_json_file_not_found(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="JSON file not found"):
        read_json_file(str(tmp_path / "non_existent.json"))


def test_read_json_file_invalid_json(tmp_path: Path):
    bad_file = tmp_path / "corrupt.json"
    bad_file.write_text("{ unquoted_key: 123 ", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid JSON in file"):
        read_json_file(str(bad_file))


def test_write_json_file_creates_nested_dirs_and_writes(tmp_path: Path):
    nested_path = tmp_path / "a" / "b" / "c" / "output.json"
    data = {"project": "ADCo", "active": True, "count": 42}

    write_json_file(str(nested_path), data)

    assert nested_path.is_file()
    recovered = read_json_file(str(nested_path))
    assert recovered == data
