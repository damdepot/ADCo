"""CLI entry point for the ADCo knob checker.

Reads a knob_tuner run directory's ``result.json``, extracts the applied
knobs, and verifies they are active on the live database (read-only).

Usage:
    uv run python -m src.knob_checker <run_dir> --db-name <db> [OPTIONS]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any

from src.knob_tuner.tools.db_connector import load_db_config
from src.knob_tuner.tools.db_tools import verify_active_knobs


def build_parser() -> argparse.ArgumentParser:
    """Construct and return the argument parser for the knob checker CLI."""
    parser = argparse.ArgumentParser(
        description="ADCo Knob Checker — verify tuner-applied knobs are live",
    )
    parser.add_argument(
        "run_dir",
        help="Path to the knob_tuner run directory containing result.json",
    )
    parser.add_argument(
        "--db-type",
        choices=["postgres", "mysql"],
        default="postgres",
        help="Database engine type (default: postgres)",
    )
    parser.add_argument(
        "--db-name",
        required=True,
        help="Name of the live database to verify knobs against",
    )
    parser.add_argument(
        "--db-config",
        default="db.config",
        help="Path to database configuration INI file (default: db.config)",
    )
    parser.add_argument(
        "--output-path",
        default="out/knob_checker/result.json",
        help="Path to write final checker result output "
        "(default: out/knob_checker/result.json)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed verification activity",
    )
    return parser


def _write_output_result(output_path: str, payload: dict[str, Any]) -> str:
    """Serialize and write the final checker outcome to the output path."""
    abs_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    return abs_path


def run_check(
    run_dir: str,
    db_type: str,
    db_name: str,
    db_config_path: str,
    verbose: bool = False,
) -> dict[str, Any]:
    """Verify the applied knobs from a tuner run against the live database.

    Args:
        run_dir: Path to the knob_tuner run directory with result.json.
        db_type: Database engine type ("postgres" or "mysql").
        db_name: Name of the live database to verify against.
        db_config_path: Path to the database configuration INI file.
        verbose: Print detailed verification activity.

    Returns:
        Result dict with timestamp, run_dir, db_type, db_name, status
        (PASS|FAIL), all_verified, knobs, and error fields.

    Raises:
        NotADirectoryError: If run_dir is not a directory.
        FileNotFoundError: If result.json or the db config file is missing.
        KeyError: If the db config section/keys or db name are missing.
        ValueError: If result.json or the db config values are invalid.
    """
    run_dir_abs = os.path.abspath(run_dir)
    if not os.path.isdir(run_dir_abs):
        raise NotADirectoryError(f"Not a directory: {run_dir_abs}")

    result_path = os.path.join(run_dir_abs, "result.json")
    if not os.path.isfile(result_path):
        raise FileNotFoundError(f"Tuner result not found: {result_path}")

    try:
        with open(result_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {result_path}: {exc}") from exc

    live_result = data.get("live_result") or {}
    if not isinstance(live_result, dict):
        live_result = {}
    applied = live_result.get("applied_knobs") or data.get("applied_knobs") or []

    normalized: list[dict[str, Any]] = []
    for item in applied:
        if not isinstance(item, dict):
            continue
        name = item.get("knob") or item.get("name")
        if not name:
            continue
        normalized.append({"name": name, "value": item.get("value")})

    if not normalized:
        return {
            "timestamp": datetime.datetime.now().isoformat(),
            "run_dir": run_dir_abs,
            "db_type": db_type,
            "db_name": db_name,
            "status": "FAIL",
            "all_verified": False,
            "knobs": [],
            "error": "no applied knobs to verify (dry-run/SKIPPED/MANUAL_SQL?)",
        }

    cfg = load_db_config(db_config_path, db_type, db_name)
    report = verify_active_knobs(cfg, normalized)

    knobs = report.get("knobs") or []
    if report.get("status") == "error":
        status = "FAIL"
    elif (
        report.get("all_verified")
        and knobs
        and all(k.get("status") == "VERIFIED" for k in knobs)
    ):
        status = "PASS"
    else:
        status = "FAIL"

    if verbose:
        for knob in knobs:
            print(
                f"  {knob.get('knob')}: {knob.get('expected_value')} "
                f"-> {knob.get('actual_value')} {knob.get('status')}"
            )

    return {
        "timestamp": datetime.datetime.now().isoformat(),
        "run_dir": run_dir_abs,
        "db_type": db_type,
        "db_name": db_name,
        "status": status,
        "all_verified": bool(report.get("all_verified", False)),
        "knobs": knobs,
        "error": report.get("error"),
    }


def main() -> None:
    """CLI main entry point."""
    parser = build_parser()
    args = parser.parse_args()

    try:
        result = run_check(
            run_dir=args.run_dir,
            db_type=args.db_type,
            db_name=args.db_name,
            db_config_path=args.db_config,
            verbose=args.verbose,
        )
    except (NotADirectoryError, FileNotFoundError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    output_path = _write_output_result(args.output_path, result)

    knobs = result.get("knobs") or []
    verified = sum(1 for k in knobs if k.get("status") == "VERIFIED")

    print("\n=== Knob Checker ===")
    print(f"Run dir:  {result.get('run_dir')}")
    print(f"DB:       {args.db_type}/{args.db_name}")
    print(f"Result:   {result.get('status')}")
    print(f"Knobs:    {verified} verified of {len(knobs)}")
    print(f"Output:   {output_path}")
    if result.get("error"):
        print(f"Error:    {result.get('error')}")

    if knobs:
        print("\nKnobs:")
        for knob in knobs:
            print(
                f"  {knob.get('knob')}: {knob.get('expected_value')} "
                f"-> {knob.get('actual_value')} {knob.get('status')}"
            )

    sys.exit(0 if result.get("status") == "PASS" else 1)


if __name__ == "__main__":
    main()
