"""Deterministic ADK ``Workflow`` for the ADCo knob_tuner pipeline.

Replaces the former LLM-orchestrated root agent with a Python-owned graph:

    START -> validate_budget -> db_inspector -> tune_loop -> apply_live -> finalize

``tune_loop`` is an async-generator :class:`FunctionNode` that owns the
recommendation/validation retry budget in Python. The LLM sub-agents only
recommend and inspect; the deterministic ``validate_plan`` tool decides
PASS/FAIL/INCONCLUSIVE, and production never restarts automatically.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any, Union

from google.adk import Context, Event, Workflow
from google.adk.models import BaseLlm
from google.adk.workflow import FunctionNode

from src.knob_tuner.contracts import (
    ApplyMode,
    KnobPlan,
    KnobScope,
    ResourceBudget,
    RunManifest,
    SysbenchProfile,
    TuningStatus,
)
from src.knob_tuner.sub_agents.db_inspector.agent import create_db_inspector_agent
from src.knob_tuner.sub_agents.knob_recommender.agent import (
    create_knob_recommender_agent,
)
from src.knob_tuner.tools.benchmark_tools import derive_screen_profile
from src.knob_tuner.tools.db_connector import DBConfig, load_db_config
from src.knob_tuner.tools.db_tools import apply_knobs, is_noop_value
from src.knob_tuner.tools.docker_tools import (
    cleanup_snapshot_image,
    resolve_docker_image,
)
from src.knob_tuner.tools.knob_scope import fetch_pg_settings_context
from src.knob_tuner.tools.knobs import (
    build_plan,
    coerce_apply_mode,
    coerce_db_config,
    coerce_profile,
    load_raw_knobs,
)
from src.knob_tuner.tools.progress import make_progress_callback
from src.knob_tuner.tools.run_artifacts import application_code_hash, write_artifact
from src.knob_tuner.tools.stats import estimate_multi_fidelity, welch_delta
from src.knob_tuner.tools.validation import SnapshotRegistry, validate_plan

DEFAULT_MODEL = "gemini-3.5-flash-lite"


def _recommendation_instruction(
    attempt: int,
    last_reasons: list[str],
    last_result: dict[str, Any],
    knob_names: list[str],
    durability_profile: str,
    workload_hint: str = "",
) -> str:
    """Build the retry-aware instruction handed to the knob_recommender agent."""
    parts = [
        "Recommend database configuration knobs for the target database.",
        "Select ONLY from these available knob names: "
        + (", ".join(knob_names) if knob_names else "(none available)"),
        f"Durability policy for this run: {durability_profile}.",
        "Use the workload, resource budget, inspector summary, and knowledge-base "
        "strategies in session state. Fetch details for your shortlist with "
        "read_knob_details before choosing values.",
    ]
    if workload_hint:
        parts.append(
            "Observed production workload context (authoritative): "
            + workload_hint.strip()
        )
    parts.append(
        "Call write_selected_knobs with your chosen recommendations; if none are "
        "justified, return an empty list."
    )
    if attempt > 1:
        parts.append(f"This is validation retry attempt {attempt}.")
        if last_reasons:
            parts.append(
                "The previous attempt failed for these reasons: "
                + "; ".join(str(reason) for reason in last_reasons)
                + "."
            )
        paired = (last_result or {}).get("paired") or {}
        if paired:
            parts.append(
                "Benchmark delta: baseline_median_tps={} tuned_median_tps={} "
                "delta_pct={} p95_delta_pct={}.".format(
                    paired.get("median_tps_baseline"),
                    paired.get("median_tps_tuned"),
                    paired.get("delta_pct"),
                    paired.get("p95_delta_pct"),
                )
            )
        parts.append(
            "Revise the recommendations to remediate the failures and avoid "
            "over-allocating memory."
        )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def validate_budget_node(ctx: Context, node_input: Any = None) -> Event:
    """Validate the resource budget before any side effect can occur."""
    raw = ctx.state.get("resource_budget")
    if raw is None:
        raise ValueError(
            "resource_budget is required in session state; pass --cpu-cores and "
            "--memory (no host auto-detection is performed)."
        )

    try:
        if isinstance(raw, ResourceBudget):
            budget = raw
        else:
            budget = ResourceBudget.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"invalid resource_budget: {exc}") from exc

    return Event(
        output=budget.model_dump(),
        state={"resource_budget": budget.model_dump()},
    )


# Under the default 'strict' durability policy these knobs may only take
# durability-preserving values. 'relaxed' lifts the restriction (but never
# fsync=off, which the prompt forbids outright).
_DURABILITY_STRICT_VALUES: dict[str, set[str]] = {
    "synchronous_commit": {"on"},
    "full_page_writes": {"on", "true", "1", "yes"},
    "fsync": {"on"},
}


def _inventory_by_name(state: Any) -> dict[str, dict[str, Any]]:
    """Index the inspected knob inventory by lowercase name."""
    inventory: dict[str, dict[str, Any]] = {}
    raw = state.get("knobs_info") or []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and entry.get("name"):
                inventory[str(entry["name"]).lower()] = entry
    return inventory


def _validate_recommendations(
    raw_knobs: list[dict[str, Any]],
    inventory: dict[str, dict[str, Any]],
    durability_profile: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep recommendations that exist in the inventory and respect constraints.

    The inventory is the trust boundary: unknown names, internal settings, enum
    violations, and durability-policy violations are rejected. Returns
    ``(valid_recommendations, rejection_reasons)``.
    """
    valid: list[dict[str, Any]] = []
    rejected: list[str] = []
    relaxed = durability_profile == "relaxed"
    for raw in raw_knobs:
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        entry = inventory.get(name.lower())
        if entry is None:
            rejected.append(f"{name}: not in available knob inventory")
            continue
        if str(entry.get("context", "")).strip().lower() == "internal":
            rejected.append(f"{name}: internal/unsettable")
            continue
        value = raw.get("value")
        enumvals = [str(e).strip().lower() for e in (entry.get("enumvals") or [])]
        if enumvals and str(value).strip().lower() not in enumvals:
            rejected.append(f"{name}: {value!r} not in {enumvals}")
            continue
        if not relaxed:
            allowed = _DURABILITY_STRICT_VALUES.get(name.lower())
            if allowed is not None and str(value).strip().lower() not in allowed:
                rejected.append(f"{name}: durability policy 'strict' forbids {value!r}")
                continue
        if is_noop_value(entry, value):
            rejected.append(
                f"{name}: {value!r} equals the current value (no-op, nothing to change)"
            )
            continue
        valid.append(raw)
    return valid, rejected


def _screen_stats(result: dict[str, Any] | None) -> dict[str, float]:
    """Welch delta between the baseline and tuned per-run samples of a result."""
    paired = (result or {}).get("paired") or {}
    baseline = (paired.get("baseline") or {}).get("per_run_tps") or []
    tuned = (paired.get("tuned") or {}).get("per_run_tps") or []
    return welch_delta([float(x) for x in baseline], [float(x) for x in tuned])


def _resolve_db_config(state: Any) -> DBConfig | None:
    """Reconstruct a live :class:`DBConfig` without a secret in session state.

    Session state persists only a redacted, JSON-serializable view (no
    password), so a usable config is rebuilt from the recorded config path. A
    fully-populated config already in state (embedded callers/tests) is honored
    first.
    """
    for key in ("db_config", "production_db_config", "prod_db_config"):
        cfg = coerce_db_config(state.get(key))
        if cfg is not None and cfg.password:
            return cfg

    path = state.get("db_config_path") or state.get("config_path")
    if isinstance(path, str) and os.path.isfile(path):
        db_type = str(state.get("db_type", "postgres") or "postgres")
        db_name = str(
            state.get("database") or state.get("db_name") or state.get("dbname") or ""
        ).strip()
        try:
            return load_db_config(path, db_type=db_type, db_override=db_name or None)
        except Exception:
            return None

    for key in ("db_config", "production_db_config", "prod_db_config"):
        cfg = coerce_db_config(state.get(key))
        if cfg is not None:
            return cfg
    return None


def make_tune_loop(recommender_node: Any, validator: Any):
    """Build the Python-owned closed-loop tune node.

    Flow: the recommender LLM selects knobs from the inspector's available-knob
    inventory; the selection is validated against that inventory, screened
    cheaply, and confirmed with fresh replicate evidence. Only a confirmed plan
    is returned for production apply. There is no deterministic candidate: if
    the LLM yields no valid plan, the run is INCONCLUSIVE and nothing is applied.

    *recommender_node* and *validator* are kept in the closure so the loop can
    be unit-tested without the ADK runtime.
    """

    async def tune_loop(
        ctx: Context, node_input: Any = None
    ) -> AsyncGenerator[Any, None]:
        state = ctx.state
        progress = make_progress_callback(
            state.get("log_file"), bool(state.get("verbose", False))
        )

        budget = ResourceBudget(**(state.get("resource_budget") or {}))
        profile = coerce_profile(state.get("sysbench_profile"))
        max_attempts = int(state.get("max_validation_attempts", 4) or 4)
        dry_run = bool(state.get("dry_run", False))
        run_id = state.get("run_id", "") or ""
        run_dir = state.get("run_dir", "") or ""
        db_type = state.get("db_type", "postgres") or "postgres"
        db_version = state.get("db_version")
        database = (
            state.get("database")
            or state.get("db_name")
            or state.get("dbname")
            or ""
        )
        benchmark_kind = str(state.get("screening_benchmark", "sysbench") or "sysbench")
        workload_hint = str(state.get("workload_hint", "") or "")
        screen_reps = max(2, int(state.get("screen_repetitions", 3) or 3))
        # Default confirmation to the hard cap so the escalation ladder is a
        # single rung. A 3-rep confirmation measurably fails by chance on a
        # working set larger than RAM (its early reps run cold), which then
        # escalates to 5 and wastes a whole screening pass; starting at 5 is
        # deterministic and costs no more than the escalated path.
        confirm_reps = max(
            screen_reps,
            int(state.get("confirm_repetitions", 5) or 5),
        )
        min_seconds = float(state.get("multi_fidelity_min_seconds", 300) or 300)
        min_improvement_pct = float(
            getattr(profile, "min_improvement_pct", 2.0)
        )

        try:
            db_config = _resolve_db_config(state)
            context_map = (
                fetch_pg_settings_context(db_config)
                if db_config is not None
                else {}
            )
        except Exception:
            context_map = {}

        # Size the sysbench screen to the target's data volume so the proxy is
        # production-shaped instead of a cache-resident toy dataset.
        schema_info = state.get("schema_info") or []
        target_total_rows = 0
        if isinstance(schema_info, list):
            for table in schema_info:
                if not isinstance(table, dict):
                    continue
                try:
                    target_total_rows += max(
                        0, int(table.get("approximate_row_count", 0) or 0)
                    )
                except (TypeError, ValueError):
                    continue
        explicit_rows = int(state.get("screen_total_rows", 0) or 0)
        if explicit_rows > 0:
            target_total_rows = explicit_rows
        max_rows = int(state.get("screen_max_rows", 5_000_000) or 5_000_000)
        screen_profile, dataset_meta = derive_screen_profile(
            profile, target_total_rows or None, max_total_rows=max_rows
        )
        if explicit_rows > 0:
            dataset_meta["source"] = "explicit"
        progress(
            f"screen dataset: {dataset_meta['total_rows']} rows "
            f"({dataset_meta['tables']}×{dataset_meta['rows_per_table']}, "
            f"source={dataset_meta['source']}"
            + (", capped" if dataset_meta.get("capped") else "")
            + ")"
        )

        # Time-fidelity: the screening pass measures a shorter window than the
        # full confirmation so a rejected candidate is failed faster. Both share
        # the same dataset size, so the prepared-dataset snapshot serves both.
        screen_seconds = int(state.get("screen_measurement_seconds", 10) or 10)
        screen_warmup = int(state.get("screen_warmup_seconds", 2) or 2)
        screen_run_profile = screen_profile.model_copy(
            update={
                "measurement_seconds": max(
                    1, min(screen_seconds, screen_profile.measurement_seconds)
                ),
                "warmup_seconds": max(
                    0, min(screen_warmup, screen_profile.warmup_seconds)
                ),
            }
        )
        confirm_run_profile = screen_profile

        # One prepared-dataset snapshot per dataset profile, reused by every
        # screening/confirmation pass in this run; deleted before the run ends.
        snapshots = SnapshotRegistry()

        def _cleanup_snapshots() -> None:
            for image in snapshots.images():
                try:
                    cleanup_snapshot_image(image)
                except Exception:
                    pass

        def _validate(
            plan: KnobPlan,
            reps: int,
            attempt: int,
            *,
            run_profile: SysbenchProfile,
            reversal: bool,
        ) -> dict[str, Any]:
            profile_for_run = run_profile.model_copy(update={"repetitions": reps})
            result = validator(
                run_id=run_id,
                run_dir=run_dir,
                plan=plan,
                budget=budget,
                profile=profile_for_run,
                db_type=db_type,
                db_version=db_version,
                database=database,
                # Staging validates the FULL plan (postmaster knobs included) by
                # restarting the throwaway container; production apply is scoped
                # separately by safe-auto / maintenance-assisted.
                apply_mode=ApplyMode.PERSIST_STATIC,
                dry_run=dry_run,
                attempt=attempt,
                benchmark_kind=benchmark_kind,
                progress=progress,
                snapshot=snapshots,
                reversal=reversal,
            )
            return result or {}

        archive: list[dict[str, Any]] = []
        last_reasons: list[str] = []

        # The inspector's inventory is the trust boundary for the LLM selection.
        inventory = _inventory_by_name(state)
        knob_names = [
            str(name) for name in (state.get("available_knob_names") or [])
        ] or sorted(inventory)
        durability_profile = (
            str(state.get("durability_profile", "strict") or "strict").strip().lower()
        )

        # One LLM candidate, retried on an invalid/empty selection. There is no
        # deterministic fallback: an empty result means the run is INCONCLUSIVE.
        candidates: list[dict[str, Any]] = []
        for attempt in range(1, max_attempts + 1):
            yield Event(
                state={
                    "validation_attempt_count": attempt,
                    "last_failure": last_reasons,
                }
            )
            progress(
                f"Attempt {attempt}/{max_attempts} — requesting knob recommendation... "
                f"({len(knob_names)} available knobs, durability={durability_profile})"
            )
            await ctx.run_node(
                recommender_node,
                node_input=_recommendation_instruction(
                    attempt,
                    last_reasons,
                    {},
                    knob_names,
                    durability_profile,
                    workload_hint,
                ),
            )
            raw_knobs = load_raw_knobs(state)
            valid_knobs, rejected = _validate_recommendations(
                raw_knobs, inventory, durability_profile
            )
            if rejected:
                last_reasons = rejected
                progress(
                    "Recommendation rejected: " + "; ".join(rejected)
                )
            plan = build_plan(valid_knobs, context_map) if valid_knobs else KnobPlan(knobs=[])
            if plan.knobs:
                candidates.append(
                    {"source": "llm", "plan": plan, "rationale": "inventory-validated"}
                )
                progress(
                    f"Recommendation received: {len(plan.knobs)} knobs "
                    f"({', '.join(spec.name for spec in plan.knobs)})"
                )
                break
            if not rejected:
                last_reasons = ["recommender returned no usable knobs"]
            progress("Recommendation received: no usable knobs")

        # Multi-fidelity is enabled only on measured economics (recorded below).
        n_confirm = 1 if candidates else 0
        gate = estimate_multi_fidelity(
            n_candidates=len(candidates),
            n_confirm=n_confirm,
            measurement_seconds=float(screen_profile.measurement_seconds),
            screen_repetitions=screen_reps,
            confirm_repetitions=confirm_reps,
            minimum_seconds=min_seconds,
            screen_seconds=float(screen_run_profile.measurement_seconds),
            confirm_seconds=float(confirm_run_profile.measurement_seconds),
        )
        multi_fidelity = bool(gate["enabled"])
        screen_reps_used = screen_reps if multi_fidelity else confirm_reps
        progress(
            f"screening {len(candidates)} candidate(s) at {screen_reps_used} rep(s) "
            f"× {screen_run_profile.measurement_seconds}s; "
            f"multi-fidelity={'on' if multi_fidelity else 'off'} "
            f"(est. saving {gate['estimated_saving_seconds']}s)"
        )

        try:
            for cand in candidates:
                plan = cand["plan"]
                t0 = time.monotonic()
                result = _validate(
                    plan,
                    screen_reps_used,
                    len(archive) + 1,
                    run_profile=screen_run_profile,
                    reversal=False,
                )
                stats = _screen_stats(result)
                cand["screen_result"] = result
                cand["screen_stats"] = stats
                entry = {
                    "candidate": cand["source"],
                    "plan_hash": plan.plan_hash(),
                    "screen": stats,
                    "screen_status": result.get("status"),
                    "screen_phase": "screen" if multi_fidelity else "full",
                    "paired": result.get("paired"),
                    "reasons": list(result.get("reasons", []) or []),
                    "wall_seconds": round(time.monotonic() - t0, 3),
                }
                archive.append(entry)
                progress(
                    f"screen {cand['source']}: delta={stats['mean_delta_pct']:.2f}% "
                    f"lcb={stats['lcb_pct']:.2f}% ucb={stats['ucb_pct']:.2f}%"
                )

            # Promotion: confirm only candidates whose upper bound does not
            # indicate likely regression; an interval entirely below zero means
            # the candidate is rejected rather than defaulted.
            eligible = [
                c for c in candidates
                if c["screen_stats"]["df"] >= 1 and c["screen_stats"]["ucb_pct"] >= 0
            ]
            eligible.sort(key=lambda c: c["screen_stats"]["lcb_pct"], reverse=True)
            for cand in eligible:
                cand["rationale"] = "screen_lcb"
            confirm_list = eligible

            # Pre-registered predicate: a valid LLM candidate is promising when
            # its mean moved up and its upper bound does not exclude zero.
            screening_promising = any(
                c["source"] == "llm"
                and c["screen_stats"]["mean_delta_pct"] > 0
                and c["screen_stats"]["ucb_pct"] >= 0
                and c.get("screen_result", {}).get("paired")
                for c in candidates
            )
            any_paired = any(
                c.get("screen_result", {}).get("paired") for c in candidates
            )

            # Escalation ladder: if screening is promising but nothing confirms,
            # raise the confirmation sample count up to the hard cap before
            # concluding INCONCLUSIVE.
            cap = max(
                screen_reps,
                int(state.get("confirmation_max_repetitions", 5) or 5),
            )
            reps_ladder = sorted({confirm_reps, cap})
            for reps in reps_ladder:
                for cand in confirm_list:
                    plan = cand["plan"]
                    # Confirmation is always a fresh, independent measurement:
                    # reusing the screening result would leave no independent
                    # evidence for promotion.
                    t0 = time.monotonic()
                    result = _validate(
                        plan,
                        reps,
                        len(archive) + 1,
                        run_profile=confirm_run_profile,
                        reversal=True,
                    )
                    stats = _screen_stats(result)
                    wall = round(time.monotonic() - t0, 3)
                    cand["confirm_result"] = result
                    cand["confirm_stats"] = stats
                    # Promotion requires BOTH the paired health check (PASS) and
                    # a positive improvement whose 95% lower confidence bound
                    # clears the configured minimum effect.
                    improvement_confident = (
                        stats["df"] >= 1
                        and stats["lcb_pct"] > min_improvement_pct
                    )
                    healthy = (
                        str(result.get("status", "")).upper()
                        == TuningStatus.PASS.value
                    )
                    cand["confirmed"] = healthy and improvement_confident
                    cand["improvement_confident"] = improvement_confident
                    archive.append(
                        {
                            "candidate": cand["source"],
                            "plan_hash": plan.plan_hash(),
                            "phase": "confirm",
                            "repetitions": reps,
                            "confirm": stats,
                            "gate_a_positive_lcb": improvement_confident,
                            "gate_b_paired_pass": healthy,
                            "confirmed": cand["confirmed"],
                            "improvement_confident": improvement_confident,
                            "paired": result.get("paired"),
                            "reasons": list(result.get("reasons", []) or []),
                            "wall_seconds": wall,
                        }
                    )
                    progress(
                        f"confirm {cand['source']} @{reps} reps: "
                        f"lcb={stats['lcb_pct']:.2f}% "
                        f"improvement_confident={improvement_confident} "
                        f"healthy={healthy} → "
                        f"{'CONFIRMED (apply)' if cand['confirmed'] else 'rejected'}"
                    )
                # Escalate only while screening is genuinely promising; a
                # negative mean is not worth more samples.
                if (
                    any(c.get("confirmed") for c in confirm_list)
                    or not screening_promising
                    or not any_paired
                ):
                    break
        except Exception as exc:
            progress(f"tune loop error: {exc}")
            last_reasons = [f"tune loop error: {exc}"]
            status = TuningStatus.FAIL
            winner = candidates[0] if candidates else None
            archive_payload: dict[str, Any] = {
                "dataset": dataset_meta,
                "durability_profile": durability_profile,
                "multi_fidelity": gate,
                "candidates": archive,
                "error": str(exc),
            }
        else:
            confirmed = [c for c in confirm_list if c.get("confirmed")]
            any_paired = any(c.get("screen_result", {}).get("paired") for c in candidates)
            if not candidates or dry_run:
                # No valid LLM plan (or dry-run): fail closed, apply nothing.
                status = TuningStatus.INCONCLUSIVE
                winner = None
            elif not any_paired:
                # Screening produced no usable benchmark evidence.
                status = TuningStatus.FAIL
                winner = None
            elif not confirm_list:
                status = TuningStatus.INCONCLUSIVE
                winner = None
            elif confirmed:
                # Prefer the strongest proven improvement (highest 95% LCB),
                # not the noisiest high-mean candidate.
                winner = max(
                    confirmed, key=lambda c: c["confirm_stats"]["lcb_pct"]
                )
                status = TuningStatus.PASS
            else:
                # Screened but not healthy (or not confirmed): fail closed.
                status = TuningStatus.INCONCLUSIVE
                winner = candidates[0]

            measurement_problem = bool(
                not dry_run
                and candidates
                and any(
                    c.get("screen_result", {}).get("paired") is None
                    for c in candidates
                )
            )
            archive_payload = {
                "dataset": dataset_meta,
                "durability_profile": durability_profile,
                "multi_fidelity": gate,
                "escalation_ladder": reps_ladder,
                "screening_promising": screening_promising,
                "min_improvement_pct": min_improvement_pct,
                "candidates": archive,
                "winner": winner["plan"].plan_hash() if winner else "",
                "measurement_problem": measurement_problem,
                "winner_improvement_confident": bool(
                    winner and winner.get("improvement_confident")
                ),
            }

        archive_payload["snapshot_images"] = snapshots.images()
        archive_payload["screen_measurement_seconds"] = (
            screen_run_profile.measurement_seconds
        )
        archive_payload["confirm_measurement_seconds"] = (
            confirm_run_profile.measurement_seconds
        )
        archive_payload["screen_reversal"] = False

        last_reasons = [
            str(r)
            for entry in archive
            for r in entry.get("reasons", [])
        ] or last_reasons

        if run_dir:
            try:
                write_artifact(run_dir, "candidate-archive", archive_payload)
            except Exception:
                pass

        _cleanup_snapshots()

        attestation = (
            (winner.get("confirm_result") or {}).get("attestation") if winner else None
        )
        winner_stats = (
            (winner.get("confirm_stats") or winner.get("screen_stats") or {})
            if winner
            else {}
        )
        winner_plan = winner["plan"] if winner else KnobPlan(knobs=[])
        summary = {
            "status": status.value,
            "run_id": run_id,
            "plan_hash": winner_plan.plan_hash(),
            "attempt_count": len(candidates),
            "reasons": last_reasons,
            "paired": (
                (winner.get("confirm_result") or winner.get("screen_result") or {}).get(
                    "paired"
                )
                if winner
                else None
            ),
            "multi_fidelity": archive_payload.get("multi_fidelity"),
            "improvement_confident": archive_payload.get(
                "winner_improvement_confident"
            ),
            "dataset": dataset_meta,
            "durability_profile": durability_profile,
        }

        yield Event(
            output=summary,
            state={
                "knob_plan": winner_plan.model_dump(),
                "knob_plan_hash": winner_plan.plan_hash(),
                "validation_attestation": attestation,
                "staging_validated": status == TuningStatus.PASS,
                "result_status": status.value,
                "validation_attempts": archive,
                "staging_issues": last_reasons,
                "candidate_archive": archive_payload,
                "multi_fidelity": archive_payload.get("multi_fidelity"),
                "confirmed_delta_pct": winner_stats.get("mean_delta_pct"),
                "improvement_confident": archive_payload.get(
                    "winner_improvement_confident"
                ),
                "screen_dataset": dataset_meta,
            },
        )

    return tune_loop


def apply_live_node(ctx: Context, node_input: Any = None) -> Event:
    """Apply the validated plan to the live database (never auto-restart)."""
    state = ctx.state
    progress = make_progress_callback(
        state.get("log_file"), bool(state.get("verbose", False))
    )
    dry_run = bool(state.get("dry_run", False))
    status = str(state.get("result_status", "") or "").upper()

    if dry_run or status != TuningStatus.PASS.value:
        live_result = {
            "status": "SKIPPED",
            "reason": (
                "dry-run: live mutations skipped"
                if dry_run
                else f"validation status is {status or 'UNKNOWN'}, not PASS"
            ),
            "applied_knobs": [],
            "persisted_static_knobs": [],
            "pending_restart_knobs": [],
        }
        progress(f"live apply skipped: {live_result['reason']}")
        return Event(
            output=live_result,
            state={"live_result": live_result, "applied_knobs": []},
        )

    cfg = _resolve_db_config(state)
    if cfg is None:
        live_result = {
            "status": "SKIPPED",
            "reason": "no live database configuration available in state",
            "applied_knobs": [],
            "persisted_static_knobs": [],
            "pending_restart_knobs": [],
        }
        progress(f"live apply skipped: {live_result['reason']}")
        return Event(
            output=live_result,
            state={"live_result": live_result, "applied_knobs": []},
        )

    raw_plan = state.get("knob_plan") or {}
    try:
        plan = (
            raw_plan
            if isinstance(raw_plan, KnobPlan)
            else KnobPlan.model_validate(raw_plan)
        )
    except Exception:
        plan = KnobPlan(knobs=[])

    mode = coerce_apply_mode(state.get("apply_mode", "dynamic"))
    knobs = [spec.model_dump() for spec in plan.knobs]

    pending_restart = [
        spec.model_dump()
        for spec in plan.knobs
        if spec.restart_required or spec.scope == KnobScope.POSTMASTER
    ]

    if str(state.get("apply_mode", "")).strip().lower() == "maintenance-assisted":
        progress("maintenance-assisted: generating manual SQL (no production mutation)...")
        try:
            sql_results = apply_knobs(knobs, cfg, dry_run=True, mode=mode)
            manual_sql = [r.get("sql", "") for r in sql_results if r.get("sql")]
            live_result = {
                "status": "MANUAL_SQL",
                "reason": "maintenance-assisted: manual SQL emitted; no auto restart",
                "applied_knobs": [],
                "persisted_static_knobs": [],
                "pending_restart_knobs": pending_restart,
                "manual_sql": manual_sql,
                "results": sql_results,
            }
        except Exception as exc:
            live_result = {
                "status": "FAILED",
                "reason": f"manual SQL generation error: {exc}",
                "applied_knobs": [],
                "persisted_static_knobs": [],
                "pending_restart_knobs": pending_restart,
                "manual_sql": [],
            }
        # Manual SQL is not an applied success: never leave result_status PASS.
        apply_status = (
            TuningStatus.INCONCLUSIVE.value
            if live_result["status"] == "MANUAL_SQL"
            else TuningStatus.FAIL.value
        )
        return Event(
            output=live_result,
            state={
                "live_result": live_result,
                "applied_knobs": [],
                "result_status": apply_status,
            },
        )

    try:
        progress(f"Applying plan to live DB ({mode.value})...")
        results = apply_knobs(knobs, cfg, dry_run=False, mode=mode)
        applied = [r for r in results if r.get("status") == "applied"]
        restart_names = {
            spec.name
            for spec in plan.knobs
            if spec.restart_required or spec.scope == KnobScope.POSTMASTER
        }
        persisted_static = [r for r in applied if r.get("knob") in restart_names]
        progress(
            f"applied {len(applied)}; pending restart: "
            f"{', '.join(sorted(restart_names)) or 'none'}"
        )
        live_result = {
            "status": "APPLIED" if applied else "COMPLETED",
            "reason": "",
            "applied_knobs": applied,
            "persisted_static_knobs": persisted_static,
            "pending_restart_knobs": pending_restart,
            "results": results,
        }
    except Exception as exc:
        live_result = {
            "status": "FAILED",
            "reason": f"live apply error: {exc}",
            "applied_knobs": [],
            "persisted_static_knobs": [],
            "pending_restart_knobs": pending_restart,
        }

    apply_state: dict[str, Any] = {
        "live_result": live_result,
        "applied_knobs": live_result["applied_knobs"],
    }
    if live_result["status"] == "FAILED":
        apply_state["result_status"] = TuningStatus.FAIL.value
    return Event(output=live_result, state=apply_state)


def finalize_node(ctx: Context, node_input: Any = None) -> Event:
    """Compose and persist the run manifest from session state."""
    state = ctx.state
    progress = make_progress_callback(
        state.get("log_file"), bool(state.get("verbose", False))
    )

    try:
        status = TuningStatus(
            str(state.get("result_status", TuningStatus.INCONCLUSIVE.value)).upper()
        )
    except ValueError:
        status = TuningStatus.INCONCLUSIVE

    # Reconcile against the live apply: a validated PASS is only a success if
    # the production mutation actually happened. A failed or absent apply must
    # never surface as a PASS manifest. MANUAL_SQL is not an applied success.
    live = state.get("live_result") or {}
    live_status = str(live.get("status", "") or "").upper()
    errors = list(state.get("staging_issues", []) or [])
    if live_status in ("FAILED", "ERROR"):
        reason = str(live.get("reason", "") or "")
        errors.append(f"live apply {live_status}: {reason}".rstrip(": "))
    if status == TuningStatus.PASS and live_status not in ("APPLIED", "COMPLETED"):
        status = (
            TuningStatus.FAIL
            if live_status in ("FAILED", "ERROR")
            else TuningStatus.INCONCLUSIVE
        )

    profile = coerce_profile(state.get("sysbench_profile"))
    target = state.get("target", "") or ""
    db_type = state.get("db_type", "") or ""
    db_version = state.get("db_version") or ""

    db_image = state.get("db_image") or ""
    if not db_image:
        try:
            db_image = resolve_docker_image(db_type, db_version)
        except Exception:
            db_image = ""

    attestation = state.get("validation_attestation") or {}
    if isinstance(attestation, dict):
        verified_knobs = attestation.get("verified_knobs", []) or []
    else:
        verified_knobs = getattr(attestation, "verified_knobs", []) or []

    attempts = state.get("validation_attempts") or []
    attempt_count = len(attempts) or int(state.get("validation_attempt_count", 0) or 0)

    progress(f"run complete: {status.value}")
    manifest = RunManifest(
        run_id=state.get("run_id", "") or "",
        timestamp=datetime.now(timezone.utc).isoformat(),
        status=status,
        resource_budget=state.get("resource_budget") or {},
        db_engine=db_type,
        db_version=str(db_version),
        db_image=db_image,
        application_target=target,
        application_code_hash=application_code_hash(target),
        knob_plan_hash=state.get("knob_plan_hash", "") or "",
        sysbench_profile_hash=profile.profile_hash(),
        seed=profile.seed,
        client_threads=profile.threads,
        attempt_count=attempt_count,
        applied_knobs=state.get("applied_knobs", []) or [],
        verified_knobs=verified_knobs,
        errors=errors,
        final_status=status.value,
    )

    return Event(
        output=manifest.model_dump(),
        state={
            "run_manifest": manifest.model_dump(),
            "result_status": status.value,
        },
    )


def create_knob_tuner_workflow(
    model: Union[str, BaseLlm] = DEFAULT_MODEL,
    buffer_time: float = 0.0,
) -> Workflow:
    """Create the deterministic ADCo knob_tuner workflow graph."""
    inspector_agent = create_db_inspector_agent(model, buffer_time)
    recommender_agent = create_knob_recommender_agent(model, buffer_time)

    tune_loop_node = FunctionNode(
        func=make_tune_loop(recommender_agent, validate_plan),
        name="tune_loop",
        rerun_on_resume=True,
    )

    return Workflow(
        name="adco_knob_tuner",
        description=(
            "ADCo knob_tuner — deterministic recommend/validate/apply workflow."
        ),
        edges=[
            (
                "START",
                validate_budget_node,
                inspector_agent,
                tune_loop_node,
                apply_live_node,
                finalize_node,
            )
        ],
    )
