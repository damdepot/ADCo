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


def _incumbent_lines(
    rows: list[dict[str, Any]], families: set[str] | None = None
) -> list[str]:
    """One-line incumbent-leader anchor for post-win steering (never throws).

    Picks the confirmed row with the best mean delta; empty when there is
    no confirmed winner yet (absent leader = generator screens normally).
    When ``families`` is non-empty, names the covered winner families so
    the generator can steer the next proposal outside them.
    """
    try:
        confirmed = [r for r in rows if isinstance(r, dict) and r.get("confirmed")]
        if not confirmed:
            return []
        best = max(confirmed, key=lambda r: _num(r.get("mean_delta_pct")))
        line = (
            f"Incumbent leader: {best.get('name', '?')} "
            f"[{best.get('phase', '?')}] "
            f"knobs={best.get('n_knobs', '?')} "
            f"mean={_fmt_pct(best.get('mean_delta_pct'))} "
            f"(vary around it; never re-propose its exact set)"
        )
        covered = sorted(f for f in (families or set()) if str(f).strip())
        if covered:
            line += (
                f" Winner families covered: {', '.join(covered)} "
                f"— include ≥1 knob outside them"
            )
        return [line]
    except Exception:  # noqa: BLE001 - bundle must never throw
        return []


def _knob_family_map(knobs_info: Any) -> dict[str, str]:
    """Index ``{lower knob name: normalized family}`` from inventory.

    Family reuses the inventory ``category`` field (no new taxonomy);
    empty/missing categories map to ``""`` (unknown). Never throws.
    """
    families: dict[str, str] = {}
    try:
        if not isinstance(knobs_info, list):
            return families
        for entry in knobs_info:
            if isinstance(entry, dict):
                name, category = entry.get("name"), entry.get("category", "")
            elif hasattr(entry, "model_dump"):
                try:
                    dumped = entry.model_dump()
                except Exception:  # noqa: BLE001, S110 - bad dumpables skip
                    continue
                if not isinstance(dumped, dict):
                    continue
                name, category = dumped.get("name"), dumped.get("category", "")
            else:
                name, category = getattr(entry, "name", None), getattr(
                    entry, "category", ""
                )
            if name is None:
                continue
            families[str(name).strip().lower()] = str(category or "").strip().lower()
    except Exception:  # noqa: BLE001 - family lookup never throws
        pass
    return families


def _names_from_knob_list(value: Any) -> list[str]:
    """Collect knob names from a knob-spec list (never throws)."""
    names: list[str] = []
    try:
        if not isinstance(value, list):
            return names
        for spec in value:
            if isinstance(spec, str):
                if spec.strip():
                    names.append(spec.strip())
            elif isinstance(spec, dict):
                for key in ("name", "knob", "knob_name"):
                    raw = spec.get(key)
                    if isinstance(raw, str) and raw.strip():
                        names.append(raw.strip())
                        break
            elif hasattr(spec, "model_dump"):
                try:
                    dumped = spec.model_dump()
                except Exception:  # noqa: BLE001, S110 - bad dumpables skip
                    continue
                if isinstance(dumped, dict):
                    for key in ("name", "knob", "knob_name"):
                        raw = dumped.get(key)
                        if isinstance(raw, str) and raw.strip():
                            names.append(raw.strip())
                            break
            else:
                raw = getattr(spec, "name", getattr(spec, "knob", None))
                if isinstance(raw, str) and raw.strip():
                    names.append(raw.strip())
    except Exception:  # noqa: BLE001 - name extraction never throws
        pass
    return names


def _plan_knobs_for_arm(state: Any, arm: str) -> list[str]:
    """Resolve an arm name to knob names via plan_hash join (never throws).

    ``experiment_history`` rows carry the arm name + ``plan_hash`` but no
    knob list; ``candidates``/``all_rows``/``last_screen_row`` carry the
    plan dumps. Joins on ``plan_hash``; ``[]`` when unresolvable.
    """
    try:
        get = getattr(state, "get", None)
        if not callable(get) or not arm:
            return []
        want = arm.strip()
        plan_hash = ""
        hist = get("experiment_history")
        if isinstance(hist, list):
            for row in hist:
                if not isinstance(row, dict):
                    continue
                for key in ("arm", "name", "exp_name"):
                    if str(row.get(key) or "").strip() == want:
                        plan_hash = str(row.get("plan_hash") or "")
                        break
                if plan_hash:
                    break
        if not plan_hash:
            return []
        pools: list[Any] = [get("candidates"), get("all_rows")]
        last = get("last_screen_row")
        if isinstance(last, dict):
            pools.append([last])
        for pool in pools:
            if not isinstance(pool, list):
                continue
            for entry in pool:
                if not isinstance(entry, dict) or entry.get("plan_hash") != plan_hash:
                    continue
                plan = entry.get("plan")
                knobs = plan.get("knobs") if isinstance(plan, dict) else None
                names = _names_from_knob_list(knobs)
                if names:
                    return names
    except Exception:  # noqa: BLE001 - arm resolution never throws
        pass
    return []


def winner_families(state: Any) -> set[str]:
    """Return families covered by registered winners (never throws).

    Reads ``state["winners"]`` (registered by the multi-winner selector;
    absent/empty means no steering). Entries may be knob-name strings or
    dicts carrying knob lists (``knobs``/``levels``/``valid_knobs``/
    ``knob_names``/``knob_list``/``verified_knobs`` keys, a ``plan`` dump,
    a single ``knob``), a precomputed ``family``/``category`` label, or arm
    references (``arm``/``exp_name``/``name`` resolved via history join).
    Families come from the inventory ``category`` field; knobs with
    unknown/missing family are skipped (pass-through, never throw).
    """
    try:
        get = getattr(state, "get", None)
        if not callable(get):
            return set()
        winners = get("winners")
        if not isinstance(winners, list) or not winners:
            return set()
        family_of = _knob_family_map(get("knobs_info"))
        covered: set[str] = set()
        names: list[str] = []
        arms: list[str] = []
        for entry in winners:
            if isinstance(entry, str):
                if entry.strip():
                    names.append(entry.strip())
                continue
            if not isinstance(entry, dict):
                continue
            for key in ("family", "knob_family", "category", "knob_category"):
                raw_family = entry.get(key)
                if isinstance(raw_family, str) and raw_family.strip():
                    covered.add(raw_family.strip().lower())
            for key in (
                "knobs",
                "levels",
                "valid_knobs",
                "knob_names",
                "knob_list",
                "verified_knobs",
            ):
                names.extend(_names_from_knob_list(entry.get(key)))
            plan = entry.get("plan")
            if isinstance(plan, dict):
                names.extend(_names_from_knob_list(plan.get("knobs")))
            elif isinstance(plan, list):
                names.extend(_names_from_knob_list(plan))
            single = entry.get("knob", entry.get("knob_name"))
            if isinstance(single, str) and single.strip():
                names.append(single.strip())
            for key in ("arm", "exp_name", "design_name"):
                raw = entry.get(key)
                if isinstance(raw, str) and raw.strip():
                    arms.append(raw.strip())
            # Bare {"name": ...} is ambiguous (arm vs knob): treat as an
            # arm only when it is not a known knob name.
            raw_name = entry.get("name")
            if isinstance(raw_name, str) and raw_name.strip():
                if raw_name.strip().lower() in family_of:
                    names.append(raw_name.strip())
                else:
                    arms.append(raw_name.strip())
        for arm in arms:
            names.extend(_plan_knobs_for_arm(state, arm))
        for n in names:
            family = family_of.get(n.strip().lower())
            if family:
                covered.add(family)
        return covered
    except Exception:  # noqa: BLE001 - winner lookup never throws
        return set()


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

    ``Winners 3/10 (lcb >= 1.0%)`` — found/target plus the certify bar, so
    the diagnosis agent can see a winner stop is premature while short of
    target. Falls back to the legacy ranking-gate key for old states.
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
    bar = state.get("certify_lcb_pct_state")
    if bar is None:
        bar = state.get("min_improvement_pct_state")
    if bar is None:
        bar = 0.0
    return f"Winners {found}/{target} (lcb >= {_num(bar):.1f}%)"


def _last_rejection_lines(state: Mapping[str, Any]) -> list[str]:
    """Render the last compile rejection as fix-first feedback (never throws)."""
    try:
        rej = state.get("last_rejection") if isinstance(state, Mapping) else None
        if hasattr(rej, "model_dump"):
            try:
                rej = rej.model_dump()
            except Exception:
                return []
        if not isinstance(rej, dict):
            return []
        reason = str(rej.get("reason", "") or "").strip()
        errors = rej.get("errors") or []
        if not isinstance(errors, list):
            errors = [errors]
        errors = [str(e).strip() for e in errors if str(e).strip()][:2]
        design = str(rej.get("design_name", "") or "").strip()
        if not reason and not errors:
            return []
        lines = ["### Last compile rejection (fix THIS violation first)"]
        head = f"- {design}: {reason}" if design else f"- {reason}"
        lines.append(head or "- rejected")
        lines.extend(f"  - {e}" for e in errors)
        return lines
    except Exception:  # noqa: BLE001 - bundle must never throw
        return []


def _shrink_ceiling_lines(state: Mapping[str, Any]) -> list[str]:
    """Active shrink-set knob-count ceiling for the candidate (never throws).

    Present only when a ``shrink_set`` diagnosis is active. ``N`` derives
    generically from the last attempt's ``n_knobs`` (history tail); when
    history is unknown it falls back to ``max_knobs + 1``. No knob names.
    """
    try:
        if not isinstance(state, Mapping):
            return []
        correction = ""
        hist = state.get("diagnosis_history")
        if isinstance(hist, list) and hist:
            last = hist[-1]
            dumped = _as_dict(last) if not isinstance(last, dict) else last
            raw = dumped.get("correction", getattr(last, "correction", ""))
            correction = getattr(raw, "value", raw)
        if not str(correction or "").strip():
            cur = state.get("diagnosis_output")
            dumped = _as_dict(cur) if not isinstance(cur, dict) else (cur or {})
            if dumped:
                raw = dumped.get("correction", getattr(cur, "correction", ""))
                correction = getattr(raw, "value", raw)
            elif cur is not None:
                raw = getattr(cur, "correction", "")
                correction = getattr(raw, "value", raw)
        if str(correction or "").strip().lower() != "shrink_set":
            return []
        n: int | None = None
        try:
            rows = _history_rows(state)
            if rows:
                n = int(rows[-1].get("n_knobs") or 0) or None
        except (TypeError, ValueError):
            n = None
        if n is None:
            try:
                ceiling = state.get("max_knobs")
                n = int(ceiling) + 1 if str(ceiling or "").strip() != "" else None
            except (TypeError, ValueError):
                n = None
        if n is None or n <= 1:
            return []
        return [
            f"Shrink ceiling: next proposal must use fewer than {n} knobs "
            f"(last attempt n_knobs={n})"
        ]
    except Exception:  # noqa: BLE001 - bundle must never throw
        return []


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
        ]
        head.extend(_shrink_ceiling_lines(state))
        head.append(f"### Experiment history ({len(rows)} runs)")
        detail_head = ["### Last experiments (detail, last 2)"]
        detail = _detail_lines(rows) or ["- none yet."]
        verdict_head = ["### Last verdict"]
        verdict = _last_verdict_lines(state) + _incumbent_lines(
            rows, winner_families(state)
        )
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
        last_rej = _last_rejection_lines(state if isinstance(state, Mapping) else {})
        while (
            len(head) + len(table) + len(detail_head) + len(detail)
            + len(verdict_head) + len(verdict) + len(rejected_head)
            + len(notes) > MAX_BUNDLE_LINES and len(notes) > 1
        ):
            del notes[0]
        # Rejection feedback is additive context (never drops history rows);
        # trim its detail lines first when over the cap.
        while (
            len(head) + len(table) + len(detail_head) + len(detail)
            + len(verdict_head) + len(verdict) + len(rejected_head)
            + len(notes) + len(last_rej) > MAX_BUNDLE_LINES and len(last_rej) > 1
        ):
            del last_rej[-1]
        lines = (
            head + table + detail_head + detail + verdict_head + verdict
            + rejected_head + notes + last_rej
        )
        return "\n".join(lines[:MAX_BUNDLE_LINES])
    except Exception as exc:  # noqa: BLE001 - bundle must never throw
        return f"## Evidence bundle\n(unavailable: {exc})"
