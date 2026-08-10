"""Convert the checker_eval dataset into a single JSON file for LLM training."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = ROOT / "datasets" / "checker_eval"
OUTPUT = DATASET_DIR / "checker_eval.json"


def _read_code_files(path: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for f in sorted(path.rglob("*.py")):
        if f.is_file() and "__pycache__" not in f.parts:
            files[f.name] = f.read_text()
    return files


def main() -> None:
    samples: list[dict[str, Any]] = []
    for sample_dir in sorted(DATASET_DIR.iterdir()):
        if not sample_dir.is_dir() or not (sample_dir / "meta.json").is_file():
            continue

        with open(sample_dir / "meta.json") as f:
            meta = json.load(f)
        with open(sample_dir / "intent.json") as f:
            intent = json.load(f)

        record: dict[str, Any] = {
            **meta,
            "intent": intent,
            "original": _read_code_files(sample_dir / "original"),
            "optimized": _read_code_files(sample_dir / "optimized"),
        }
        samples.append(record)

    with open(OUTPUT, "w") as f:
        json.dump(samples, f, indent=2, ensure_ascii=False)

    print(f"Converted {len(samples)} samples → {OUTPUT}")


if __name__ == "__main__":
    main()
