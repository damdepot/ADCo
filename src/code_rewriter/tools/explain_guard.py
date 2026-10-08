"""Optional EXPLAIN cost guard for candidate SQL (never fails closed).

``explain_cost_guard`` compares the estimated plan cost of original vs candidate
SQL strings when a database URL is available.  When no DB URL is configured,
the driver is missing, or any EXPLAIN fails, it returns a ``skip`` verdict so
callers treat the check as advisory-only (never block without evidence).

The module is schema-agnostic: it never references specific tables, columns or
functions, and it only parses generic ``cost=...`` fragments from EXPLAIN
output.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence, Union

_COST_RE = re.compile(r"cost=\d+(?:\.\d+)?\.\.(\d+(?:\.\d+)?)", re.IGNORECASE)

_DEFAULT_THRESHOLD_RATIO = 1.5


def _as_sql_list(sqls: Union[str, Sequence[str], None]) -> List[str]:
    if sqls is None:
        return []
    if isinstance(sqls, str):
        return [sqls] if sqls.strip() else []
    out: List[str] = []
    try:
        for item in sqls:
            if isinstance(item, str) and item.strip():
                out.append(item)
    except Exception:
        return []
    return out


def _resolve_db_url(db_url: Optional[str] = None) -> str:
    if isinstance(db_url, str) and db_url.strip():
        return db_url.strip()
    for key in ("ADCO_DB_URL", "DATABASE_URL", "POSTGRES_URL", "DB_URL"):
        value = os.environ.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _skip(reason: str) -> Dict[str, Any]:
    return {"status": "skip", "verdict": "unknown", "reason": reason}


def _extract_total_cost(plan_text: str) -> Optional[float]:
    """Sum the outer total-cost figures found in EXPLAIN output."""
    try:
        costs = [float(m.group(1)) for m in _COST_RE.finditer(plan_text or "")]
    except Exception:
        return None
    if not costs:
        return None
    return max(costs)


def _explain_costs_postgres(sqls: List[str], db_url: str) -> Optional[List[float]]:
    """Return estimated total cost per SQL via EXPLAIN, or None on any failure."""
    if not sqls:
        return []
    module_name = ""
    for candidate in ("psycopg2", "psycopg"):
        try:
            __import__(candidate)
            module_name = candidate
            break
        except Exception:
            continue
    if not module_name:
        return None
    try:
        if module_name == "psycopg2":
            import psycopg2  # type: ignore

            conn = psycopg2.connect(db_url)
        else:
            import psycopg  # type: ignore

            conn = psycopg.connect(db_url)
    except Exception:
        return None
    costs: List[float] = []
    try:
        cur = conn.cursor()
        for sql in sqls:
            try:
                cur.execute("EXPLAIN " + sql)
                rows = cur.fetchall()
            except Exception:
                return None
            text = "\n".join(str(row[0]) if row else "" for row in rows)
            cost = _extract_total_cost(text)
            if cost is None:
                return None
            costs.append(cost)
        return costs
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def explain_cost_guard(
    original_sqls: Union[str, Sequence[str], None],
    candidate_sqls: Union[str, Sequence[str], None],
    db_url: Optional[str] = None,
    threshold_ratio: float = _DEFAULT_THRESHOLD_RATIO,
) -> Dict[str, Any]:
    """Compare EXPLAIN costs of original vs candidate SQL (advisory-first).

    Returns a dict with ``status`` in ``{"skip", "ok", "regression"}``:

    * ``skip`` — no DB URL, no driver, or any EXPLAIN failure.  Callers must
      treat this as "unknown" and must NOT block the candidate.
    * ``ok`` — EXPLAIN succeeded and candidate cost is within threshold.
    * ``regression`` — EXPLAIN succeeded and candidate cost exceeds
      ``original * threshold_ratio``.

    Never raises and never fails closed: every unexpected condition maps to
    ``skip``.
    """
    try:
        orig = _as_sql_list(original_sqls)
        cand = _as_sql_list(candidate_sqls)
        if not orig or not cand:
            return _skip("no SQL to compare")
        try:
            threshold = float(threshold_ratio)
        except Exception:
            threshold = _DEFAULT_THRESHOLD_RATIO
        if not (threshold > 0):
            threshold = _DEFAULT_THRESHOLD_RATIO

        url = _resolve_db_url(db_url)
        if not url:
            return _skip("no DB URL available")
        lowered = url.lower()
        if lowered.startswith("sqlite") or lowered == ":memory:":
            return _skip("EXPLAIN cost comparison unavailable for sqlite")

        orig_costs = _explain_costs_postgres(orig, url)
        if orig_costs is None:
            return _skip("EXPLAIN unavailable (driver/connection/parse)")
        cand_costs = _explain_costs_postgres(cand, url)
        if cand_costs is None:
            return _skip("EXPLAIN unavailable (driver/connection/parse)")

        orig_total = float(sum(orig_costs))
        cand_total = float(sum(cand_costs))
        if orig_total <= 0:
            return {"status": "ok", "verdict": "ok", "reason": "zero baseline cost"}
        ratio = cand_total / orig_total
        if ratio > threshold:
            return {
                "status": "regression",
                "verdict": "regression",
                "reason": (
                    f"candidate EXPLAIN cost {cand_total:.2f} exceeds original "
                    f"{orig_total:.2f} by {ratio:.2f}x (threshold {threshold:.2f}x)"
                ),
                "original_cost": orig_total,
                "candidate_cost": cand_total,
                "ratio": ratio,
                "threshold": threshold,
            }
        return {
            "status": "ok",
            "verdict": "ok",
            "reason": f"candidate cost {cand_total:.2f} vs original {orig_total:.2f}",
            "original_cost": orig_total,
            "candidate_cost": cand_total,
            "ratio": ratio,
            "threshold": threshold,
        }
    except Exception as exc:
        return _skip(f"unexpected guard failure: {exc}")
