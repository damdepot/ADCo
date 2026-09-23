"""Structured repair-request models for the optimizer feedback loop.

A deterministic verification result (``VerificationResult.model_dump()``) is
mapped into a flat, ordered list of :class:`RepairIssue` objects. The optimizer
sees a compact, stable markdown repair request instead of a terse free-text
failure blurb.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_LOOP_BATCH_HINT = (
    "Remove every DB call from the loop. Hoist ONE set-based batch read per "
    "table before the loop and batch writes with executemany after it. Do NOT "
    "emulate batching with a Python loop that issues one query per group: for a "
    "multi-column key use a single composite-key statement (e.g. "
    "`WHERE (a, b) IN ((%s, %s), ...)` or `a = ANY(%s) AND b IN (...)`)."
)

_FIX_HINTS: dict[str, str] = {
    "STRATEGY_NOT_APPLIED": _LOOP_BATCH_HINT,
    "MISSING_REWRITE": _LOOP_BATCH_HINT,
    "DUPLICATE_WHERE": "Rebuild the full statement; do not append a second WHERE.",
    "MULTI_STATEMENT_EXECUTE": "One execute() per statement.",
    "UNKNOWN_QUERY_KEY": "Reuse an existing query key verbatim.",
    "ROW_INDEX_OUT_OF_RANGE": "Include the key column(s) in the SELECT or fix the row indices.",
    "IMPLICIT_CROSS_JOIN": "Replace the implicit comma join of 3+ tables with explicit JOIN ... ON (or a scalar subquery).",
    "PERCENT_FORMAT_ARITY": "Never %-format a string that contains %s placeholders. Pre-format only the dynamic identifier into a variable, then build the SQL with an f-string and pass values as execute() params.",
    "UNDEFINED_NAME": "Bind the name. In a comprehension always write `for <name> in <iterable>`; otherwise add the missing assignment or parameter.",
    "SLOW_EXECUTEMANY": "Use psycopg2.extras.execute_batch (or execute_values) for bulk writes instead of cursor.executemany.",
}


class RepairIssue(BaseModel):
    """A single, actionable issue the optimizer must repair."""

    code: str
    severity: str
    file: str = ""
    function: str = ""
    line: int | None = None
    message: str
    expected: Any = None
    actual: Any = None
    fix_hint: str = ""
    evidence: str = ""


def build_repair_issues(verification: dict) -> list[RepairIssue]:
    """Map a ``VerificationResult.model_dump()`` into an ordered issue list."""
    issues: list[RepairIssue] = []
    if not isinstance(verification, dict):
        return issues

    for violation in verification.get("violations") or []:
        if not isinstance(violation, dict):
            continue
        code = str(violation.get("code") or "")
        issues.append(
            RepairIssue(
                code=code,
                severity=str(violation.get("severity") or "ERROR"),
                file=str(violation.get("file") or ""),
                function=str(violation.get("function") or ""),
                line=violation.get("line"),
                message=str(violation.get("message") or ""),
                expected=violation.get("expected"),
                actual=violation.get("actual"),
                fix_hint=_FIX_HINTS.get(code, ""),
                evidence=str(violation.get("evidence") or ""),
            )
        )

    for entry in verification.get("target_coverage") or []:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if status == "TRANSFORMED":
            continue
        code = str(status or "")
        issues.append(
            RepairIssue(
                code=code,
                severity="ERROR",
                file=str(entry.get("file") or ""),
                function=str(entry.get("function") or ""),
                message=str(entry.get("details") or ""),
                fix_hint=_FIX_HINTS.get(code, ""),
            )
        )

    return issues


def render_repair_request(issues: list[RepairIssue]) -> str:
    """Render a deterministic markdown repair request grouped by function."""
    lines = ["## Repair Request"]
    if not issues:
        lines.append("(no issues)")
        return "\n".join(lines)

    grouped: dict[str, list[RepairIssue]] = {}
    for issue in issues:
        grouped.setdefault(issue.function or "(general)", []).append(issue)

    for function in sorted(grouped):
        lines.append(f"### {function}")
        for issue in grouped[function]:
            lines.append(f"- [{issue.code}] {issue.message}")
            lines.append(
                f"  Definition of done: {issue.fix_hint or '(no specific guidance)'}"
            )
    return "\n".join(lines)
