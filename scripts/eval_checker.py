"""Evaluate the ADCo checker against the checker_eval dataset.

Usage:
    uv run python scripts/eval_checker.py [--data datasets/checker_eval]
        [--model gemini-3.5-flash-lite] [--limit N] [--delay SEC]
        [--verbose] [--no-strict]

Results are printed to stdout as summary tables and saved as CSV/JSON under
scripts/results/checker_eval_<timestamp>.{csv,json}
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.code_checker.main
RESULTS_DIR = ROOT / "scripts" / "results"
DEFAULT_MODEL = "gemini-3.5-flash-lite"

VALID_STATUSES = ("PASS", "WARN", "FAIL")


def main() -> None:
    args = parse_args()
    data_dir = Path(args.data)
    samples = discover_samples(data_dir)
    if args.category:
        samples = [s for s in samples if load_meta(s).get("category") == args.category]
    if args.limit > 0:
        samples = samples[: args.limit]

    if args.dry_run:
        print_plan(samples)
        return

    print(f"\n{'#' * 60}")
    print(f"# CHECKER EVALUATION")
    print(f"# Model: {args.model}  Data: {data_dir}  Delay: {args.delay}s")
    if args.category:
        print(f"# Category filter: {args.category}")
    print(f"{'#' * 60}")

    results: list[dict[str, Any]] = []
    for i, sample_dir in enumerate(samples):
        print(f"\n>>> Sample {i + 1}/{len(samples)}: {sample_dir.name}")
        if i > 0 and args.delay > 0:
            print(f"Sleeping {args.delay}s ...")
            time.sleep(args.delay)
        results.append(run_sample(sample_dir, model=args.model, verbose=args.verbose))

    print_table(results)
    metrics = print_summary(results)
    confusion = print_confusion(results)
    print_responses(results)
    print_totals(results)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path, json_path = save_results(results, metrics, confusion, args, ts)
    print(f"Results saved to: {csv_path}")
    print(f"Results saved to: {json_path}")

    if any(not r["match"] for r in results) and not args.no_strict:
        sys.exit(1)
    sys.exit(0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the ADCo checker against the checker_eval dataset",
    )
    parser.add_argument(
        "--data", default="datasets/checker_eval",
        help="Dataset root directory (default: datasets/checker_eval)",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Gemini model for the checker (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Only run the first N samples (sorted by dir name); 0 = all",
    )
    parser.add_argument(
        "--delay", type=float, default=0.0,
        help="Cooldown seconds between samples (default: 0)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print detailed checker agent activity",
    )
    parser.add_argument(
        "--no-strict", action="store_true",
        help="Exit 0 even when samples mismatch (default: exit 1)",
    )
    parser.add_argument(
        "--category", default="",
        help="Only evaluate samples of this category (e.g. correctness, safety)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def discover_samples(data_dir: Path) -> list[Path]:
    if not data_dir.is_dir():
        print(f"ERROR: not a directory: {data_dir}", file=sys.stderr)
        sys.exit(2)
    samples: list[Path] = []
    for entry in sorted(data_dir.iterdir()):
        if entry.is_dir() and (entry / "meta.json").is_file():
            samples.append(entry)
    return samples


def load_meta(sample_dir: Path) -> dict[str, Any]:
    try:
        with open(sample_dir / "meta.json") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def print_plan(samples: list[Path]) -> None:
    print(f"\n{'=' * 90}")
    print(f"DRY RUN — {len(samples)} samples discovered")
    print(f"{'=' * 90}")
    for d in samples:
        meta = load_meta(d)
        expected = meta.get("expected_status", "?")
        if meta.get("polarity") == "neg":
            expected += f" [{meta.get('expected_category') or '?'}]"
        print(
            f"  {d.name:<44} {meta.get('polarity', '?'):<5} "
            f"{meta.get('category', '?'):<22} expected={expected:<14} "
            f"name={meta.get('name', '?')}"
        )


def run_sample(sample_dir: Path, model: str, verbose: bool) -> dict[str, Any]:
    meta = load_meta(sample_dir)
    record: dict[str, Any] = {
        "sample": sample_dir.name,
        "id": meta.get("id", ""),
        "name": meta.get("name", ""),
        "category": meta.get("category", ""),
        "domain": meta.get("domain", ""),
        "polarity": meta.get("polarity", ""),
        "expected_status": meta.get("expected_status", ""),
        "expected_category": meta.get("expected_category") or "",
        "actual_status": "",
        "issue_categories": "",
        "detected_categories": [],
        "match": False,
        "mismatch_type": "",
        "summary": "",
        "error": "",
        "duration_s": 0.0,
    }

    started = time.time()
    try:
        state = asyncio.run(
            src.code_checker.main._run_checker(
                str(sample_dir / "optimized"),
                original=str(sample_dir / "original"),
                model=model,
                verbose=verbose,
            )
        )
        status, issues, summary = parse_output(state)
    except Exception as exc:
        record["actual_status"] = "ERROR"
        record["mismatch_type"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["duration_s"] = time.time() - started
        print(f"  ERROR: {record['error']}")
        return record

    categories = issue_categories(issues)
    record.update(
        actual_status=status,
        issue_categories=", ".join(categories) or "(none)",
        detected_categories=categories,
        summary=summary,
        duration_s=time.time() - started,
    )
    score_sample(record, meta, status, categories)
    mark = "✓" if record["match"] else "✗"
    print(
        f"  → {record['actual_status']}  categories: {record['issue_categories']}  "
        f"match: {mark}  ({record['duration_s']:.1f}s)"
    )
    if record["summary"]:
        print(f"    checker summary: {record['summary']}")
    return record


def parse_output(state: dict[str, Any]) -> tuple[str, list[Any], str]:
    """Normalize checker_output from dict or pydantic form; raise if unparseable."""
    output = state.get("checker_output")
    if isinstance(output, dict):
        status = output.get("status") or ""
        if status not in VALID_STATUSES:
            raise ValueError(f"unexpected checker status {status!r}")
        return status, output.get("issues") or [], output.get("summary") or ""
    if output is not None:
        return output.status, output.issues, output.summary
    raise ValueError("checker_output missing from checker state")


def issue_categories(issues: list[Any]) -> list[str]:
    cats: set[str] = set()
    for issue in issues:
        if isinstance(issue, dict):
            cat = issue.get("category")
        else:
            cat = getattr(issue, "category", None)
        if cat:
            cats.add(str(cat))
    return sorted(cats)


def score_sample(
    record: dict[str, Any], meta: dict[str, Any], status: str, categories: list[str]
) -> None:
    polarity = meta.get("polarity")
    expected_category = record["expected_category"]
    if polarity == "pos":
        if status == "PASS":
            record["match"] = True
        else:
            record["mismatch_type"] = f"false_positive({status})"
    elif status == "FAIL" and expected_category in categories:
        record["match"] = True
    elif status in ("PASS", "WARN"):
        record["mismatch_type"] = "missed"
    else:
        record["mismatch_type"] = "wrong category"


# ─── reporting ───────────────────────────────────────────────────────────────

def print_table(results: list[dict[str, Any]]) -> None:
    print(f"\n{'=' * 140}")
    print("PER-SAMPLE RESULTS")
    print(f"{'=' * 140}")
    header = (
        f"{'sample':<44} {'pol':<5} {'category':<22} {'expected':<9} "
        f"{'actual':<9} {'issue categories':<46} {'match':<22}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        mark = "✓" if r["match"] else "✗"
        if r["mismatch_type"]:
            mark += f" ({r['mismatch_type']})"
        print(
            f"{r['sample']:<44} {r['polarity']:<5} {r['category']:<22} "
            f"{r['expected_status']:<9} {r['actual_status'] or '-':<9} "
            f"{r['issue_categories'] or '-':<46} {mark}"
        )
    print("-" * len(header))


def print_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    matched = sum(1 for r in results if r["match"])
    pos = [r for r in results if r["polarity"] == "pos"]
    neg = [r for r in results if r["polarity"] == "neg"]
    errors = [r for r in results if r["error"]]

    metrics: dict[str, Any] = {
        "total": total,
        "matched": matched,
        "accuracy": round(matched / total, 4) if total else 0.0,
        "errors": [r["sample"] for r in errors],
    }

    print(f"\n{'=' * 85}")
    print("EVAL SUMMARY")
    print(f"{'=' * 85}")
    print(f"Samples:            {total}")
    print(f"Matched:            {matched}/{total} ({matched / total:.1%})")
    if errors:
        print(f"Errors:             {len(errors)} ({', '.join(r['sample'] for r in errors)})")

    if pos:
        pos_m = sum(1 for r in pos if r["match"])
        fp = len(pos) - pos_m
        print(f"\nPositives:          {pos_m}/{len(pos)} passed")
        print(f"False-positive rate:{fp}/{len(pos)} ({fp / len(pos):.1%})")
        for r in pos:
            if not r["match"]:
                print(f"  - {r['sample']} → {r['mismatch_type']}")
        metrics["positives"] = {
            "total": len(pos),
            "passed": pos_m,
            "false_positives": fp,
            "false_positive_rate": round(fp / len(pos), 4),
        }

    if neg:
        neg_m = sum(1 for r in neg if r["match"])
        print(f"\nNegatives:          {neg_m}/{len(neg)} detected with correct category")
        by_category: dict[str, dict[str, int]] = {}
        for c in sorted({r["category"] for r in neg}):
            grp = [r for r in neg if r["category"] == c]
            m = sum(1 for r in grp if r["match"])
            by_category[c] = {"total": len(grp), "detected": m}
            print(f"  {c:<22} {m}/{len(grp)}")
        missed = [r for r in neg if r["mismatch_type"] == "missed"]
        wrong = [r for r in neg if r["mismatch_type"] == "wrong category"]
        if missed:
            print(f"Missed:             {', '.join(r['name'] or r['sample'] for r in missed)}")
        if wrong:
            print(f"Wrong category:     {', '.join(r['name'] or r['sample'] for r in wrong)}")
        metrics["negatives"] = {
            "total": len(neg),
            "detected_correct_category": neg_m,
            "detection_rate": round(neg_m / len(neg), 4),
            "by_category": by_category,
            "missed": [r["name"] or r["sample"] for r in missed],
            "wrong_category": [r["name"] or r["sample"] for r in wrong],
        }

    return metrics


def print_confusion(results: list[dict[str, Any]]) -> dict[str, Any]:
    neg_fail = [
        r for r in results if r["polarity"] == "neg" and r["actual_status"] == "FAIL"
    ]
    row_cats = sorted({r["category"] for r in results if r["polarity"] == "neg"})
    col_cats = sorted({c for r in neg_fail for c in r["detected_categories"]})
    matrix = {rc: {cc: 0 for cc in col_cats} for rc in row_cats}
    for r in neg_fail:
        for c in r["detected_categories"]:
            matrix[r["category"]][c] += 1

    print(f"\n{'=' * 85}")
    print("CONFUSION MATRIX (negatives, status FAIL only)")
    print("rows: expected category, columns: detected categories")
    print(f"{'=' * 85}")
    if not neg_fail:
        print("  (no FAIL samples)")
    else:
        header = (
            f"{'expected \\ detected':<22}"
            + "".join(f"{c[:14]:<16}" for c in col_cats)
            + f"{'#fail':<6}{'matched':<8}"
        )
        print(header)
        print("-" * len(header))
        for rc in row_cats:
            counts = [matrix[rc][cc] for cc in col_cats]
            n_fail = sum(1 for r in neg_fail if r["category"] == rc)
            n_match = sum(1 for r in neg_fail if r["category"] == rc and r["match"])
            print(
                f"{rc:<22}"
                + "".join(f"{str(v):<16}" for v in counts)
                + f"{n_fail:<6}{n_match:<8}"
            )
        print("-" * len(header))

    return {"rows": row_cats, "columns": col_cats, "counts": matrix}


def print_responses(results: list[dict[str, Any]]) -> None:
    print(f"\n{'=' * 85}")
    print("CHECKER RESPONSES")
    print(f"{'=' * 85}")
    for r in results:
        summary = (r.get("summary") or "").strip()
        if not summary:
            summary = "(no response)"
        print(f"\n── {r['sample']} ──")
        print(f"   status: {r['actual_status'] or '-'}  categories: {r['issue_categories'] or '-'}")
        print(f"   response: {summary}")


def print_totals(results: list[dict[str, Any]]) -> None:
    total = len(results)
    matched = sum(1 for r in results if r["match"])
    pos = [r for r in results if r["polarity"] == "pos"]
    neg = [r for r in results if r["polarity"] == "neg"]
    parts = []
    if pos:
        pos_m = sum(1 for r in pos if r["match"])
        parts.append(f"{pos_m}/{len(pos)} positives passed")
    if neg:
        neg_m = sum(1 for r in neg if r["match"])
        parts.append(f"{neg_m}/{len(neg)} negatives detected with correct category")
    errors = [r for r in results if r["error"]]
    suffix = f" ({len(errors)} errors)" if errors else ""
    print(f"\nTOTALS: {matched}/{total} samples matched ({', '.join(parts)}){suffix}")


# ─── saving ──────────────────────────────────────────────────────────────────

def save_results(
    results: list[dict[str, Any]],
    metrics: dict[str, Any],
    confusion: dict[str, Any],
    args: argparse.Namespace,
    ts: str,
) -> tuple[Path, Path]:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    csv_path = RESULTS_DIR / f"checker_eval_{ts}.csv"
    json_path = RESULTS_DIR / f"checker_eval_{ts}.json"

    fieldnames = [
        "sample", "id", "name", "category", "domain", "polarity",
        "expected_status", "expected_category", "actual_status",
        "issue_categories", "detected_categories", "match", "mismatch_type",
        "summary", "error", "duration_s",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            row = {
                k: (",".join(v) if isinstance(v, list) else v)
                for k, v in r.items()
                if k in fieldnames
            }
            writer.writerow(row)

    payload = {
        "args": {k: str(v) for k, v in vars(args).items()},
        "timestamp": ts,
        "metrics": metrics,
        "confusion": confusion,
        "results": results,
    }
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2)

    return csv_path, json_path


if __name__ == "__main__":
    main()
