"""File utility tools for reading and writing JSON files."""

import json
import os
from pathlib import Path
from typing import Any


def read_json_file(path: str) -> dict[str, Any] | list[Any]:
    """Read and parse a JSON file.

    Args:
        path: File path to the JSON file.

    Returns:
        Parsed JSON content as dict or list.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If file content is invalid JSON.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"JSON file not found: {path}")

    try:
        content = file_path.read_text(encoding="utf-8")
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in file '{path}': {e}") from e


def write_json_file(path: str, data: dict[str, Any] | list[Any]) -> None:
    """Write data to a JSON file with pretty formatting.

    Creates any missing parent directories automatically.

    Args:
        path: Destination file path.
        data: Data structure (dict or list) to serialize.
    """
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
