"""Structured repair-request models for the optimizer feedback loop.

A deterministic verification result (``VerificationResult.model_dump()``) is
mapped into a flat, ordered list of :class:`RepairIssue` objects. The optimizer
sees a compact, stable markdown repair request instead of a terse free-text
failure blurb.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

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
    "FRAGILE_COMPOSITE_AGG": "Never ARRAY_AGG(ROW(...)) and parse the text; use json_agg/jsonb_agg, select columns separately, or keep the second query.",
    "COMPOSITE_ANY_ARRAY": "Do not pass a list of tuples to = ANY(%s); use `(a, b) IN ((%s, %s), ...)` with flattened parameters.",
    "LOOKUP_KEY_NOT_SELECTED": "Include the batched filter key column(s) in the SELECT projection before keying the lookup dict.",
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


class OptimizerAttempt(BaseModel):
    """One optimizer attempt on a target function, persisted for feedback."""

    file: str = ""
    function: str = ""
    bare_function: str = ""
    outcome: str = ""
    codes: list[str] = []
    message: str = ""
    attempt: int | None = None
    candidate_key: str = ""
    diff: str = ""


_ATTEMPT_DIFF_MAX_LINES = 40


def _bare_name(function: str) -> str:
    """Strip a qualified name to its bare function component."""
    return (function or "").strip().rsplit(".", 1)[-1]


def _truncate_diff(diff: str, max_lines: int = _ATTEMPT_DIFF_MAX_LINES) -> str:
    """Keep at most ``max_lines`` lines of a diff, marking truncation."""
    lines = diff.splitlines()
    if len(lines) <= max_lines:
        return diff
    return "\n".join(lines[:max_lines]) + "\n... (diff truncated)"


def record_optimizer_attempt(
    state: dict, attempt: OptimizerAttempt, cap: int = 12
) -> None:
    """Append an attempt, truncating its diff and capping history per target."""
    try:
        if not attempt.bare_function:
            attempt.bare_function = _bare_name(attempt.function)
        attempt.diff = _truncate_diff(attempt.diff)
        entries = state.setdefault("optimizer_attempts", [])
        if not isinstance(entries, list):
            entries = []
            state["optimizer_attempts"] = entries
        entries.append(attempt.model_dump())

        key = (attempt.file, attempt.bare_function)
        matching = [
            index
            for index, entry in enumerate(entries)
            if isinstance(entry, dict)
            and (entry.get("file"), entry.get("bare_function")) == key
        ]
        for index in matching[: max(0, len(matching) - cap)][::-1]:
            entries.pop(index)
    except Exception:
        return None


def render_optimizer_attempts(
    state: dict, file: str, function: str, limit: int = 3
) -> str:
    """Render the most recent prior attempts for a target, newest first."""
    target = _bare_name(function)
    raw = state.get("optimizer_attempts")
    if not isinstance(raw, list):
        return ""

    matching: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("file") != file:
            continue
        bare = entry.get("bare_function") or _bare_name(entry.get("function") or "")
        if bare != target:
            continue
        matching.append(entry)

    if not matching:
        return ""

    recent = matching[-limit:]
    lines = ["## Prior Optimizer Attempts (do not repeat these)"]
    for offset, entry in enumerate(reversed(recent)):
        index = len(recent) - offset
        number = entry.get("attempt")
        label = number if number is not None else index
        codes = entry.get("codes")
        codes_text = ", ".join(codes) if isinstance(codes, list) else ""
        message = entry.get("message") or ""
        lines.append(f"- attempt {label}: {entry.get('outcome') or ''} [{codes_text or 'NO_CODE'}] {message}")
        diff = entry.get("diff") or ""
        if diff:
            lines.append("  ```diff")
            lines.extend(f"  {line}" for line in diff.splitlines())
            lines.append("  ```")
    return "\n".join(lines)


def count_optimizer_attempts(state: dict, file: str, function: str) -> int:
    """Count recorded attempts for a target, matching on (file, bare function)."""
    target = _bare_name(function)
    raw = state.get("optimizer_attempts")
    if not isinstance(raw, list):
        return 0
    count = 0
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("file") != file:
            continue
        bare = entry.get("bare_function") or _bare_name(entry.get("function") or "")
        if bare == target:
            count += 1
    return count
