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
from src.knob_tuner.tools.experiments import (
    format_protocol_feedback,
    pick_winner,
    run_experiment_arms,
)
from src.knob_tuner.tools.knob_scope import fetch_pg_settings_context
from src.knob_tuner.tools.knobs import (
    build_plan,
    coerce_apply_mode,
    coerce_db_config,
    coerce_profile,
)
from src.knob_tuner.tools.progress import make_progress_callback
from src.knob_tuner.tools.run_artifacts import application_code_hash, write_artifact
from src.knob_tuner.tools.stats import welch_delta
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
    """Build the single-experiment instruction handed to the knob_recommender agent."""
    parts = [
        "Propose ONE database configuration experiment for this attempt.",
        "Select ONLY from these available knob names: "
        + (", ".join(knob_names) if knob_names else "(none available)"),
        f"Durability policy for this run: {durability_profile}.",
        (
            "Write your design as a single experiment to state keys "
            "next_experiment / experiment_design_output with shape "
            "{name, phase, levels: [{knob, value, reasoning}], rationale, "
            "objective}: exactly one experiment per attempt."
        ),
        (
            "Phase must be one of screen, interaction, refinement "
            "(case-insensitive; anything else is rejected): screen explores "
            "a broad set of knobs; interaction tests combinations of known "
            "movers; refinement fine-tunes known movers. No experiment may "
            "exceed 20 distinct knobs (larger sets are rejected) — keep each "
            "experiment attributable and cheap."
        ),
        (
            "Budget: at most 6 experiments per run (state max_experiments); "
            "each attempt proposes exactly one next experiment — do not "
            "propose multi-arm batches."
        ),
        (
            "Use the workload, resource budget, inspector summary, and knowledge-base "
            "strategies in session state. You MUST fetch details for your shortlist with "
            "read_knob_details before choosing values — never guess a current value "
            "and never copy a default from the guardrails as the current value."
        ),
    ]
    if workload_hint:
        parts.append(
            "Observed production workload context (authoritative): "
            + workload_hint.strip()
        )
    parts.append(
        "If no experiment is justified, return an empty design "
        "(no usable knobs)."
    )
    if attempt > 1 or last_reasons:
        parts.append(f"This is experiment attempt {attempt}.")
        if last_reasons:
            parts.append(
                "Accumulated experiment history and rejections so far "
                "(full history — every prior experiment and every rejected "
                "value, live current value shown where known): "
                + "; ".join(str(reason) for reason in last_reasons)
                + "."
            )
            parts.append(
                "Do NOT repeat a rejected value: a recommendation identical to "
                "its live current value is a no-op and will be rejected again. "
                "Do not repeat a tested experiment: vary failed/rejected "
                "experiments with fresh knobs or values. Either pick a different "
                "knob or a value that differs from the live current value shown above."
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
            "Revise the single next experiment to remediate the failures and avoid "
            "over-allocating memory."
        )
    return " ".join(parts)


_VALID_EXPERIMENT_PHASES = ("screen", "interaction", "refinement")


def _read_next_experiment(state: Any) -> Any | None:
    """Return the raw single-experiment design from session state, if any.

    Reads ``state["next_experiment"]`` first, then
    ``state["experiment_design_output"]``. Both accept a dict with keys
    name/phase/levels/rationale/objective or an object with those
    attributes. Returns ``None`` when neither key holds a single-experiment
    design (legacy multi-arm ``arms`` payloads do not count).
    """
    for key in ("next_experiment", "experiment_design_output"):
        try:
            raw = state.get(key)
        except AttributeError:
            return None
        if raw is None:
            continue
        if isinstance(raw, dict):
            if "arms" in raw and "levels" not in raw and "name" not in raw:
                continue
            if any(k in raw for k in ("name", "phase", "levels")):
                return raw
            if key == "next_experiment":
                return raw
            continue
        if any(hasattr(raw, attr) for attr in ("name", "phase", "levels")):
            return raw
    return None


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
        max_experiments = max(1, int(state.get("max_experiments", 6) or 6))
        _raw_max_attempts = state.get("max_validation_attempts", None)
        if _raw_max_attempts is None:
            max_attempts = max_experiments
        else:
            try:
                max_attempts = min(
                    max_experiments, max(1, int(_raw_max_attempts or max_experiments))
                )
            except (TypeError, ValueError):
                max_attempts = max_experiments
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
        # Single-fidelity short runs: many 10s repetitions give the Welch
        # screen more degrees of freedom than a few long ones, and losers
        # stop early for futility instead of paying a full confirmation.
        candidate_reps = max(2, int(state.get("candidate_repetitions", 10) or 10))
        candidate_seconds = max(
            1, int(state.get("candidate_measurement_seconds", 10) or 10)
        )
        candidate_warmup = max(
            0, int(state.get("candidate_warmup_seconds", 2) or 2)
        )
        early_stop_min_reps = max(
            2, int(state.get("early_stop_min_reps", 4) or 4)
        )
        # Candidates are validated as single-knob-set experiments: sets with
        # more distinct knobs than this cap are rejected without spending a
        # benchmark run, keeping each experiment attributable and cheap.
        max_set_knobs = max(1, int(state.get("max_set_knobs", 20) or 20))
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

        # One short-run profile for every arm of the run: the shared baseline
        # and each candidate measure the same window, so samples are directly
        # comparable. The prepared-dataset snapshot serves all of them.
        candidate_run_profile = screen_profile.model_copy(
            update={
                "repetitions": candidate_reps,
                "measurement_seconds": max(1, candidate_seconds),
                "warmup_seconds": max(0, candidate_warmup),
            }
        )

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
            attempt: int,
            *,
            run_profile: SysbenchProfile,
            shared_baseline: Any = None,
            baseline_only: bool = False,
        ) -> dict[str, Any]:
            result = validator(
                run_id=run_id,
                run_dir=run_dir,
                plan=plan,
                budget=budget,
                profile=run_profile,
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
                reversal=False,
                shared_baseline=shared_baseline,
                baseline_only=baseline_only,
                early_stop_min_reps=early_stop_min_reps,
            )
            return result or {}

        archive: list[dict[str, Any]] = []
        last_reasons: list[str] = []
        candidates: list[dict[str, Any]] = []
        # Rejections accumulate across attempts so the recommender keeps seeing
        # every stale/no-op value (with its live current value), instead of the
        # feedback being wiped whenever an attempt comes back empty.
        rejected_history: list[str] = []
        # Sequential experimental design: one executed experiment per attempt.
        experiment_history: list[dict[str, Any]] = []
        # Every executed single-arm validation row, across attempts.
        all_rows: list[dict[str, Any]] = []
        experiments_run = 0

        # The inspector's inventory is the trust boundary for the LLM selection.
        inventory = _inventory_by_name(state)
        knob_names = [
            str(name) for name in (state.get("available_knob_names") or [])
        ] or sorted(inventory)
        durability_profile = (
            str(state.get("durability_profile", "strict") or "strict").strip().lower()
        )

        # Single-fidelity short runs: measure the shared baseline once, then
        # run sequential single-knob-set experiments against it. Losers stop
        # early for futility; strong winners break early. Anything confirmed
        # but not strong keeps the loop going until the budget is exhausted,
        # when the best confirmed experiment wins. There is no deterministic
        # fallback: no usable evidence means the run is INCONCLUSIVE (or FAIL
        # when no paired evidence was ever produced) and nothing is applied.
        winner: dict[str, Any] | None = None
        status = TuningStatus.INCONCLUSIVE
        shared_baseline: Any = None
        try:
            progress(
                f"measuring shared baseline at {candidate_reps} rep(s) "
                f"× {candidate_run_profile.measurement_seconds}s..."
            )
            baseline_result = _validate(
                KnobPlan(knobs=[]),
                0,
                run_profile=candidate_run_profile,
                baseline_only=True,
            )
            shared_baseline = baseline_result.get("baseline")
            if isinstance(shared_baseline, dict):
                baseline_tps = list(shared_baseline.get("per_run_tps") or [])
            else:
                baseline_tps = list(
                    getattr(shared_baseline, "per_run_tps", None) or []
                )
            archive.append(
                {
                    "phase": "baseline",
                    "status": baseline_result.get("status"),
                    "repetitions": len(baseline_tps),
                    "reasons": list(baseline_result.get("reasons", []) or []),
                }
            )
            if baseline_result.get("status") != "ok" or not baseline_tps:
                last_reasons = list(baseline_result.get("reasons", []) or []) or [
                    "shared baseline produced no usable evidence"
                ]
                progress("shared baseline failed — failing the run")
                status = TuningStatus.FAIL
            elif dry_run:
                last_reasons = ["dry-run: validation skipped (no mutations)"]
                progress("dry-run: skipping candidate validation")
                status = TuningStatus.INCONCLUSIVE
            else:
                for attempt in range(1, max_attempts + 1):
                    yield Event(
                        state={
                            "validation_attempt_count": attempt,
                            "last_failure": last_reasons,
                        }
                    )
                    progress(
                        f"Attempt {attempt}/{max_attempts} — requesting single experiment... "
                        f"({len(knob_names)} available knobs, durability={durability_profile})"
                    )
                    history_lines = [
                        "Experiment {} [{}]: n_knobs={} mean={:+.2f}% "
                        "lcb={:+.2f}% status={} confirmed={}".format(
                            h.get("name"),
                            h.get("phase"),
                            h.get("n_knobs"),
                            float(h.get("mean_delta_pct", 0.0)),
                            float(h.get("lcb_pct", 0.0)),
                            h.get("status"),
                            bool(h.get("confirmed", False)),
                        )
                        for h in experiment_history
                    ]
                    combined_reasons = list(rejected_history) + history_lines
                    await ctx.run_node(
                        recommender_node,
                        node_input=_recommendation_instruction(
                            attempt,
                            combined_reasons or last_reasons,
                            {},
                            knob_names,
                            durability_profile,
                            workload_hint,
                        ),
                    )
                    # Each attempt the LLM proposes ONE experiment, read
                    # DIRECTLY from state — never depend on any tool having
                    # run. `next_experiment` wins; `experiment_design_output`
                    # (dict or attribute object) is the fallback.
                    design = _read_next_experiment(state)
                    if design is None:
                        note = (
                            "recommender returned no usable knobs "
                            "(no experiment design)"
                        )
                        if note not in rejected_history:
                            rejected_history.append(note)
                        last_reasons = list(rejected_history)
                        progress(
                            "Experiment received: no usable knobs "
                            "(no experiment design)"
                        )
                        continue
                    if isinstance(design, dict):
                        exp_name = str(
                            design.get("name") or f"experiment-{attempt}"
                        )
                        phase_raw = design.get("phase") or ""
                        raw_levels = design.get("levels") or []
                        exp_rationale = str(design.get("rationale") or "")
                        exp_objective = str(
                            design.get("objective")
                            or design.get("summary")
                            or ""
                        )
                    else:
                        exp_name = str(
                            getattr(design, "name", "")
                            or f"experiment-{attempt}"
                        )
                        phase_raw = getattr(design, "phase", "") or ""
                        raw_levels = getattr(design, "levels", []) or []
                        exp_rationale = str(
                            getattr(design, "rationale", "") or ""
                        )
                        exp_objective = str(
                            getattr(design, "objective", None)
                            or getattr(design, "summary", None)
                            or ""
                        )
                    norm_levels: list[dict[str, Any]] = []
                    for lvl in raw_levels or []:
                        if isinstance(lvl, dict):
                            if lvl.get("value") is not None:
                                lvl_value = lvl.get("value")
                            else:
                                lvl_value = lvl.get("recommended_value")
                            lvl_knob = lvl.get(
                                "knob",
                                lvl.get("name", lvl.get("knob_name", "")),
                            )
                            lvl_reasoning = str(
                                lvl.get("reasoning", "") or ""
                            )
                            lvl_restart = bool(lvl.get("restart_required", False))
                        else:
                            lvl_knob = getattr(
                                lvl,
                                "knob",
                                getattr(
                                    lvl, "name", getattr(lvl, "knob_name", "")
                                ),
                            )
                            lvl_value = getattr(
                                lvl,
                                "value",
                                getattr(lvl, "recommended_value", None),
                            )
                            lvl_reasoning = str(
                                getattr(lvl, "reasoning", "") or ""
                            )
                            lvl_restart = bool(
                                getattr(lvl, "restart_required", False)
                            )
                        if not lvl_knob or lvl_value is None:
                            continue
                        norm_levels.append(
                            {
                                "knob": str(lvl_knob),
                                "value": lvl_value,
                                "reasoning": lvl_reasoning,
                                "restart_required": lvl_restart,
                            }
                        )
                    if not norm_levels:
                        note = (
                            f"experiment {exp_name!r}: no usable knobs "
                            "(empty levels)"
                        )
                        if note not in rejected_history:
                            rejected_history.append(note)
                        last_reasons = list(rejected_history)
                        progress(
                            f"Experiment {exp_name!r} received: no usable knobs "
                            "(empty levels)"
                        )
                        continue
                    phase = str(phase_raw or "").strip().lower()
                    if phase not in _VALID_EXPERIMENT_PHASES:
                        note = (
                            f"experiment {exp_name!r}: unknown phase "
                            f"{phase_raw!r}"
                        )
                        if note not in rejected_history:
                            rejected_history.append(note)
                        last_reasons = list(rejected_history)
                        progress(
                            f"Experiment {exp_name!r} rejected: unknown phase "
                            f"{phase_raw!r}"
                        )
                        continue
                    distinct = {
                        str(lvl["knob"]).lower() for lvl in norm_levels
                    }
                    if len(distinct) > max_set_knobs:
                        note = (
                            f"experiment {exp_name!r}: distinct knobs "
                            f"{len(distinct)} above cap {max_set_knobs}"
                        )
                        if note not in rejected_history:
                            rejected_history.append(note)
                        last_reasons = list(rejected_history)
                        progress(
                            f"Experiment {exp_name!r} rejected: " + note
                        )
                        continue
                    raw_for_validation = [
                        {
                            "name": lvl["knob"],
                            "value": lvl["value"],
                            "restart_required": lvl.get("restart_required", False),
                        }
                        for lvl in norm_levels
                    ]
                    valid_knobs, inv_rejected = _validate_recommendations(
                        raw_for_validation, inventory, durability_profile
                    )
                    if inv_rejected:
                        for reason in inv_rejected:
                            if str(reason) not in rejected_history:
                                rejected_history.append(str(reason))
                    if not valid_knobs:
                        last_reasons = list(rejected_history) or [
                            f"experiment {exp_name!r}: no usable knobs"
                        ]
                        progress(
                            f"Experiment {exp_name!r} received: no usable knobs"
                        )
                        continue
                    plan = build_plan(valid_knobs, context_map)
                    if not plan.knobs:
                        note = f"experiment {exp_name!r}: no usable knobs"
                        if note not in rejected_history:
                            rejected_history.append(note)
                        last_reasons = list(rejected_history)
                        progress(
                            f"Experiment {exp_name!r} received: no usable knobs"
                        )
                        continue
                    progress(
                        f"Experiment received: {exp_name} [{phase}] "
                        f"{len(plan.knobs)} knobs "
                        f"({', '.join(spec.name for spec in plan.knobs)})"
                    )

                    def _arm_validate(
                        *,
                        plan: KnobPlan,
                        run_profile: Any,
                        shared_baseline: Any,
                        attempt: int,
                        early_stop_min_reps: Any = None,
                        **_kwargs: Any,
                    ) -> dict[str, Any]:
                        return _validate(
                            plan,
                            attempt,
                            run_profile=run_profile,
                            shared_baseline=shared_baseline,
                        )

                    t0 = time.monotonic()
                    rows = run_experiment_arms(
                        arms=[(plan, phase, exp_name)],
                        shared_baseline=shared_baseline,
                        validate_fn=_arm_validate,
                        run_profile=candidate_run_profile,
                        attempt_base=len(archive),
                        early_stop_min_reps=early_stop_min_reps,
                        progress=progress,
                        min_improvement_pct=min_improvement_pct,
                    )
                    wall = round(time.monotonic() - t0, 3)
                    for row in rows:
                        row_stats = {
                            "mean_delta_pct": float(
                                row.get("mean_delta_pct", 0.0)
                            ),
                            "lcb_pct": float(row.get("lcb_pct", 0.0)),
                            "ucb_pct": float(row.get("ucb_pct", 0.0)),
                            "df": float(row.get("df", 0.0)),
                        }
                        row_healthy = (
                            str(row.get("status", "")).upper()
                            == TuningStatus.PASS.value
                        )
                        tuned_reps = int(row.get("reps", 0) or 0)
                        archive.append(
                            {
                                "candidate": "llm",
                                "plan_hash": row.get("plan_hash"),
                                "phase": row.get("phase"),
                                "status": row.get("status"),
                                "stats": row_stats,
                                "repetitions": tuned_reps,
                                "stopped_early": bool(
                                    row.get("stopped_early", False)
                                ),
                                "gate_a_positive_lcb": bool(
                                    row.get("improvement_confident", False)
                                ),
                                "gate_b_paired_pass": row_healthy,
                                "confirmed": bool(row.get("confirmed", False)),
                                "improvement_confident": bool(
                                    row.get("improvement_confident", False)
                                ),
                                "paired": row.get("paired"),
                                "reasons": list(row.get("reasons", []) or []),
                                "wall_seconds": wall,
                                "arm": row.get("arm"),
                            }
                        )
                        candidates.append(
                            {
                                "source": "llm",
                                "plan": row.get("plan"),
                                "rationale": exp_rationale or "single-experiment",
                                "result": row.get("result"),
                                "stats": row_stats,
                                "confirmed": bool(row.get("confirmed", False)),
                                "improvement_confident": bool(
                                    row.get("improvement_confident", False)
                                ),
                            }
                        )
                        experiment_history.append(
                            {
                                "name": exp_name,
                                "phase": phase,
                                "n_knobs": len(plan.knobs),
                                "mean_delta_pct": row_stats["mean_delta_pct"],
                                "lcb_pct": row_stats["lcb_pct"],
                                "status": row.get("status"),
                                "confirmed": bool(row.get("confirmed", False)),
                            }
                        )
                        progress(
                            f"arm {row.get('arm')} [{row.get('phase')}] "
                            f"@{tuned_reps} reps: "
                            f"delta={row_stats['mean_delta_pct']:.2f}% "
                            f"lcb={row_stats['lcb_pct']:.2f}% "
                            f"improvement_confident="
                            f"{bool(row.get('improvement_confident', False))} "
                            f"healthy={row_healthy} → "
                            f"{'CONFIRMED (apply)' if row.get('confirmed') else 'rejected'}"
                        )
                    all_rows.extend(rows)
                    experiments_run += 1
                    row_reasons = [
                        str(r)
                        for row in rows
                        for r in (row.get("reasons") or [])
                    ]
                    for reason in row_reasons:
                        if reason not in rejected_history:
                            rejected_history.append(reason)
                    feedback = format_protocol_feedback(
                        exp_objective or exp_name, rows
                    )
                    if feedback not in rejected_history:
                        rejected_history.append(feedback)
                    last_reasons = list(rejected_history)
                    # STRONG WIN: healthy PASS with LCB above the minimum
                    # improvement stops the loop immediately.
                    strong_win: dict[str, Any] | None = None
                    for row in rows:
                        try:
                            row_lcb = float(row.get("lcb_pct", 0.0))
                        except (TypeError, ValueError):
                            row_lcb = 0.0
                        if (
                            str(row.get("status", "")).upper()
                            == TuningStatus.PASS.value
                            and bool(row.get("confirmed", False))
                            and row_lcb > min_improvement_pct
                        ):
                            strong_win = row
                            break
                    if strong_win is not None:
                        winner = next(
                            (
                                c
                                for c in candidates
                                if c.get("plan") is strong_win.get("plan")
                            ),
                            None,
                        )
                        if winner is None:
                            win_stats = {
                                "mean_delta_pct": float(
                                    strong_win.get("mean_delta_pct", 0.0)
                                ),
                                "lcb_pct": float(
                                    strong_win.get("lcb_pct", 0.0)
                                ),
                                "ucb_pct": float(
                                    strong_win.get("ucb_pct", 0.0)
                                ),
                                "df": float(strong_win.get("df", 0.0)),
                            }
                            winner = {
                                "source": "llm",
                                "plan": strong_win.get("plan"),
                                "rationale": exp_rationale or "single-experiment",
                                "result": strong_win.get("result"),
                                "stats": win_stats,
                                "confirmed": True,
                                "improvement_confident": bool(
                                    strong_win.get(
                                        "improvement_confident", False
                                    )
                                ),
                            }
                        status = TuningStatus.PASS
                        break
                else:
                    # Budget exhausted without a strong win: the best
                    # confirmed experiment (by mean delta) wins.
                    best_row = pick_winner(
                        [row for row in all_rows if row.get("confirmed")]
                    )
                    if best_row is not None:
                        winner = next(
                            (
                                c
                                for c in candidates
                                if c.get("plan") is best_row.get("plan")
                            ),
                            None,
                        )
                        if winner is None:
                            win_stats = {
                                "mean_delta_pct": float(
                                    best_row.get("mean_delta_pct", 0.0)
                                ),
                                "lcb_pct": float(
                                    best_row.get("lcb_pct", 0.0)
                                ),
                                "ucb_pct": float(
                                    best_row.get("ucb_pct", 0.0)
                                ),
                                "df": float(best_row.get("df", 0.0)),
                            }
                            winner = {
                                "source": "llm",
                                "plan": best_row.get("plan"),
                                "rationale": "single-experiment",
                                "result": best_row.get("result"),
                                "stats": win_stats,
                                "confirmed": True,
                                "improvement_confident": bool(
                                    best_row.get(
                                        "improvement_confident", False
                                    )
                                ),
                            }
                        status = TuningStatus.PASS
                    else:
                        ever_paired = bool(baseline_tps) or any(
                            row.get("paired") is not None for row in all_rows
                        )
                        if ever_paired:
                            status = TuningStatus.INCONCLUSIVE
                        else:
                            last_reasons = list(rejected_history) or [
                                "no paired evidence produced"
                            ]
                            status = TuningStatus.FAIL

            def _cand_paired(cand: dict[str, Any]) -> Any:
                res = cand.get("result")
                if isinstance(res, dict):
                    return res.get("paired")
                return getattr(res, "paired", None)

            measurement_problem = bool(
                not dry_run
                and candidates
                and any(_cand_paired(c) is None for c in candidates)
            )
            archive_payload: dict[str, Any] = {
                "dataset": dataset_meta,
                "durability_profile": durability_profile,
                "single_fidelity": {
                    "enabled": True,
                    "candidate_repetitions": candidate_reps,
                    "candidate_seconds": candidate_run_profile.measurement_seconds,
                    "candidate_warmup_seconds": candidate_run_profile.warmup_seconds,
                    "early_stop_min_reps": early_stop_min_reps,
                },
                "min_improvement_pct": min_improvement_pct,
                "candidates": archive,
                "winner": winner["plan"].plan_hash() if winner else "",
                "measurement_problem": measurement_problem,
                "winner_improvement_confident": bool(
                    winner and winner.get("improvement_confident")
                ),
                "experiment_history": experiment_history,
                "experiments_run": experiments_run,
            }
        except Exception as exc:
            progress(f"tune loop error: {exc}")
            last_reasons = [f"tune loop error: {exc}"]
            status = TuningStatus.FAIL
            winner = candidates[0] if candidates else None
            archive_payload = {
                "dataset": dataset_meta,
                "durability_profile": durability_profile,
                "single_fidelity": {
                    "enabled": True,
                    "candidate_repetitions": candidate_reps,
                    "candidate_seconds": candidate_run_profile.measurement_seconds,
                    "candidate_warmup_seconds": candidate_run_profile.warmup_seconds,
                    "early_stop_min_reps": early_stop_min_reps,
                },
                "min_improvement_pct": min_improvement_pct,
                "candidates": archive,
                "winner": winner["plan"].plan_hash() if winner else "",
                "measurement_problem": False,
                "winner_improvement_confident": bool(
                    winner and winner.get("improvement_confident")
                ),
                "experiment_history": experiment_history,
                "experiments_run": experiments_run,
                "error": str(exc),
            }

        archive_payload["snapshot_images"] = snapshots.images()
        archive_payload["candidate_measurement_seconds"] = (
            candidate_run_profile.measurement_seconds
        )
        archive_payload["candidate_repetitions"] = candidate_reps
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
            (winner.get("result") or {}).get("attestation") if winner else None
        )
        winner_stats = (winner.get("stats") or {}) if winner else {}
        winner_plan = winner["plan"] if winner else KnobPlan(knobs=[])
        summary = {
            "status": status.value,
            "run_id": run_id,
            "plan_hash": winner_plan.plan_hash(),
            "attempt_count": experiments_run,
            "reasons": last_reasons,
            "paired": (
                (winner.get("result") or {}).get("paired") if winner else None
            ),
            "single_fidelity": archive_payload.get("single_fidelity"),
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
                "single_fidelity": archive_payload.get("single_fidelity"),
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
