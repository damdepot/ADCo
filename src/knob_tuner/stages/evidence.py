"""Compact quantitative evidence bundle for the diagnosis→generator loop.

Pure function of a state mapping. Revives the intent of the retired
``format_protocol_feedback`` summarizer (see
``src/knob_tuner/tools/experiments.py``) as bounded markdown both agents
can read: experiment table, last-verdict deltas, resources, attempt line.
Output is capped (~40 lines); older history rows are truncated first.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.knob_tuner.contracts import get_max_attempts, get_validation_attempt

MAX_BUNDLE_LINES = 40


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_pct(value: Any) -> str:
    """Format a percentage, or ``"n/a"`` when there is no measurement.

    A missing mean must never render as ``+0.00%`` — that confuses "failed"
    with "measured zero".
    """
    if value is None:
        return "n/a"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:  # noqa: BLE001, S110 - non-dict dumpables stringify downstream
            pass
    return {}


def _history_rows(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("experiment_history") if isinstance(state, Mapping) else []
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            rows.append(entry)
    return rows


def _attempt(state: Mapping[str, Any]) -> tuple[str, str]:
    # Phase 4.1/4.3: canonical loop counter + cap from contracts.
    try:
        attempt_s = str(int(get_validation_attempt(state)))
    except (TypeError, ValueError):
        attempt_s = "0"
    total = get_max_attempts(state)
    try:
        total_s = str(int(total)) if total is not None else "?"
    except (TypeError, ValueError):
        total_s = str(total)
    return attempt_s, total_s


def _resource_line(state: Mapping[str, Any]) -> str:
    budget = _as_dict(state.get("resource_budget"))
    # ResourceBudget declares cpu_cores; older states used cpu/cpus/cpu_count.
    cpu = budget.get(
        "cpu_cores",
        budget.get("cpu", budget.get("cpus", budget.get("cpu_count", "?"))),
    )
    mem = budget.get("memory_gb", budget.get("memory", state.get("memory_gb", "?")))
    try:
        mem_s = f"{float(mem):g}GB"
    except (TypeError, ValueError):
        mem_s = str(mem) if mem not in (None, "") else "unknown"
    cpu_s = str(cpu) if cpu not in (None, "") else "unknown"
    return f"Resources: cpu={cpu_s} memory={mem_s}"


def _fmt_pwin(value: Any) -> str:
    try:
        if value is None:
            return "n/a"
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _history_table_lines(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| # | name | phase | knobs | mean% | lcb% | status | confirmed | p(win) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, row in enumerate(rows, start=1):
        lines.append(
            f"| {i} | {row.get('name', '')} | {row.get('phase', '')} | "
            f"{row.get('n_knobs', '')} | "
            f"{_fmt_pct(row.get('mean_delta_pct'))} | "
            f"{_fmt_pct(row.get('lcb_pct'))} | "
            f"{row.get('status', '')} | "
            f"{'yes' if row.get('confirmed') else 'no'} | "
            f"{_fmt_pwin(row.get('p_win'))} |"
        )
    return lines


def _detail_lines(rows: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in rows[-2:]:
        lines.append(
            f"- {row.get('name', '?')} [{row.get('phase', '?')}] "
            f"knobs={row.get('n_knobs', '?')} "
            f"mean={_fmt_pct(row.get('mean_delta_pct'))} "
            f"lcb={_fmt_pct(row.get('lcb_pct'))} "
            f"status={row.get('status', '?')} "
            f"confirmed={'yes' if row.get('confirmed') else 'no'}"
        )
    return lines


def _last_verdict_pwin(row: dict[str, Any]) -> str:
    """P(win) for the last verdict: stored value first, else paired samples."""
    stored = row.get("p_win")
    if stored is not None:
        return _fmt_pwin(stored)
    try:
        from src.knob_tuner.tools.stats import p_win as _pwin

        def _samples(key: str) -> list[float]:
            paired = row.get("paired")
            side = (paired or {}).get(key) if isinstance(paired, dict) else None
            raw = (side or {}).get("per_run_tps") if isinstance(side, dict) else None
            if not isinstance(raw, (list, tuple)):
                return []
            out: list[float] = []
            for item in raw:
                try:
                    out.append(float(item))
                except (TypeError, ValueError):
                    continue
            return out

        base, tuned = _samples("baseline"), _samples("tuned")
        if len(base) >= 2 and len(tuned) >= 2:
            return _fmt_pwin(_pwin(base, tuned))
    except Exception:  # noqa: BLE001, S110 - bundle must never throw
        pass
    return "n/a"


def _incumbent_lines(rows: list[dict[str, Any]]) -> list[str]:
    """One-line incumbent-leader anchor for post-win steering (never throws).

    Picks the confirmed row with the best mean delta; empty when there is
    no confirmed winner yet (absent leader = generator screens normally).
    """
    try:
        confirmed = [r for r in rows if isinstance(r, dict) and r.get("confirmed")]
        if not confirmed:
            return []
        best = max(confirmed, key=lambda r: _num(r.get("mean_delta_pct")))
        return [
            f"Incumbent leader: {best.get('name', '?')} "
            f"[{best.get('phase', '?')}] "
            f"knobs={best.get('n_knobs', '?')} "
            f"mean={_fmt_pct(best.get('mean_delta_pct'))} "
            f"(vary around it; never re-propose its exact set)"
        ]
    except Exception:  # noqa: BLE001 - bundle must never throw
        return []


def _last_verdict_lines(state: Mapping[str, Any]) -> list[str]:
    row = state.get("last_screen_row")
    if not isinstance(row, dict):
        return ["Last verdict: none yet."]
    reasons = row.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = [reasons]
    reason_s = "; ".join(str(r) for r in reasons[:3]) or "n/a"
    name = row.get("arm", row.get("exp_name", row.get("name", "?")))
    lines = [
        (
            f"Last verdict: {name} [{row.get('phase', '?')}] "
            f"status={row.get('status', '?')}"
        ),
        (
            f"  mean={_fmt_pct(row.get('mean_delta_pct'))} "
            f"lcb={_fmt_pct(row.get('lcb_pct'))} "
            f"ucb={_fmt_pct(row.get('ucb_pct'))} "
            f"df={_num(row.get('df', 0.0)):.1f} "
            f"p(win)={_last_verdict_pwin(row)} "
            f"confirmed={'yes' if row.get('confirmed') else 'no'}"
        ),
        f"  reasons: {reason_s}",
    ]
    plan_hash = row.get("plan_hash", state.get("plan_hash", ""))
    if plan_hash:
        lines.append(f"  plan_hash: {plan_hash}")
    return lines


def _winners_line(state: Mapping[str, Any]) -> str:
    """Render the compounding-campaign winner progress line.

    ``Winners 3/10 (lcb >= 5.0%)`` — found/target plus the bar, so the
    diagnosis agent can see a winner stop is premature while short of target.
    Never throws.
    """
    try:
        found = int(state.get("winners_found", 0) or 0)
    except (TypeError, ValueError):
        found = 0
    try:
        target = int(state.get("success_candidates_target", 0) or 0)
    except (TypeError, ValueError):
        target = 0
    min_pct = state.get("min_improvement_pct_state")
    if min_pct is None:
        min_pct = 0.0
    return f"Winners {found}/{target} (lcb >= {_num(min_pct):.1f}%)"


def build_evidence_bundle(state: Mapping[str, Any] | None) -> str:
    """Render a capped markdown evidence bundle from a state mapping."""
    try:
        if not isinstance(state, Mapping):
            state = {}
        attempt_s, total_s = _attempt(state)
        rows = _history_rows(state)

        rejected = state.get("rejected_history") or []
        if not isinstance(rejected, list):
            rejected = [rejected]
        rejected = [str(r) for r in rejected if str(r).strip()]

        # Fixed (non-history) sections.
        head = [
            "## Evidence bundle",
            f"Attempt {attempt_s}/{total_s}",
            _winners_line(state),
            _resource_line(state),
            f"### Experiment history ({len(rows)} runs)",
        ]
        detail_head = ["### Last experiments (detail, last 2)"]
        detail = _detail_lines(rows) or ["- none yet."]
        verdict_head = ["### Last verdict"]
        verdict = _last_verdict_lines(state) + _incumbent_lines(rows)
        rejected_head = ["### Rejected notes"]
        # History table is the first thing truncated; rejected notes second.
        table = (
            _history_table_lines(rows)
            if rows
            else ["(no experiments yet)"]
        )
        omitted = 0
        while len(head) + len(table) + len(detail_head) + len(detail) + len(
            verdict_head
        ) + len(verdict) + len(rejected_head) + min(len(rejected), 5) + (
            1 if omitted else 0
        ) > MAX_BUNDLE_LINES and len(table) > 3:
            # Drop the oldest data row (index 2, after header+separator).
            del table[2]
            omitted += 1
        if omitted:
            table.append(f"... +{omitted} older run(s) omitted (cap {MAX_BUNDLE_LINES} lines)")
        notes = [f"- {r}" for r in rejected[-5:]] or ["- none."]
        while (
            len(head) + len(table) + len(detail_head) + len(detail)
            + len(verdict_head) + len(verdict) + len(rejected_head)
            + len(notes) > MAX_BUNDLE_LINES and len(notes) > 1
        ):
            del notes[0]
        lines = (
            head + table + detail_head + detail + verdict_head + verdict
            + rejected_head + notes
        )
        return "\n".join(lines[:MAX_BUNDLE_LINES])
    except Exception as exc:  # noqa: BLE001 - bundle must never throw
        return f"## Evidence bundle\n(unavailable: {exc})"
