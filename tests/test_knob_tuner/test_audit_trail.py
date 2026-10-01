"""Phase 2 audit-trail completeness: manifest.json answers "what changed, and why".

Covers: verified_knobs/timings surviving the result-pop, attempt_count from
validation_attempt_count, experiments_run increments, rejected-history merge,
controller routing in decision output, pending_restart_knobs in the manifest,
crash-stub artifacts, builder parity, and structured/deduped staging_issues.
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.knob_tuner.contracts import KnobPlan
from src.knob_tuner.main import _manifest_from_state, run_pipeline
from src.knob_tuner.stages import nodes as stage_nodes
from src.knob_tuner.stages.models import CompiledPlan, CompileRejection
from src.knob_tuner.workflow import (
    confirmation_controller_node,
    decision_node,
    finalize_node,
)


class _FakeContext:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state if state is not None else {}
        self.route = None


def _validate_with_attestation(**kwargs):
    return {
        "status": "PASS",
        "paired": {
            "baseline": {"per_run_tps": [100.0, 101.0, 102.0, 99.0, 100.5]},
            "tuned": {"per_run_tps": [120.0, 121.0, 119.0, 122.0, 120.5]},
        },
        "reasons": [],
        "stopped_early": False,
        "attestation": {
            "database_identity": "postgres://127.0.0.1:5555/testdb",
            "verified_knobs": [
                {"knob": "work_mem", "status": "VERIFIED", "actual_value": "64MB"}
            ],
        },
        "artifacts": {"timings": {"baseline_seconds": 12.5, "apply_seconds": 0.4}},
    }


def _compiled():
    plan = KnobPlan.model_validate(
        {"knobs": [{"name": "work_mem", "value": "64MB", "scope": "user"}]}
    )
    return CompiledPlan(
        plan=plan.model_dump(),
        exp_name="e1",
        phase="screen",
        valid_knobs=["work_mem"],
    )


def test_screen_persists_attestation_and_timings_past_result_pop():
    ctx = _FakeContext({"min_improvement_pct": 5.0})
    stage_nodes.screen_candidate(
        ctx, _compiled(), validate_fn=_validate_with_attestation
    )
    # Attestation surface: manifest readers see verified_knobs.
    assert ctx.state["validation_attestation"]["verified_knobs"] == [
        {"knob": "work_mem", "status": "VERIFIED", "actual_value": "64MB"}
    ]
    # The stripped lite row keeps the auditable surface, not the blob.
    lite = ctx.state["last_screen_row"]
    assert "result" not in lite
    assert lite["verified_knobs"] == [
        {"knob": "work_mem", "status": "VERIFIED", "actual_value": "64MB"}
    ]
    assert lite["timings"] == {"baseline_seconds": 12.5, "apply_seconds": 0.4}
    assert ctx.state["validation_timings"] == {
        "baseline_seconds": 12.5,
        "apply_seconds": 0.4,
    }
    # The committed history row carries its own timings.
    assert ctx.state["experiment_history"][0]["timings"] == {
        "baseline_seconds": 12.5,
        "apply_seconds": 0.4,
    }


def test_finalize_attempt_count_uses_counter_not_history_length(tmp_path: Path):
    target = tmp_path / "app"
    target.mkdir()
    (target / "main.py").write_text("print('hi')\n")
    ctx = _FakeContext(
        {
            "run_id": "run-abc",
            "result_status": "INCONCLUSIVE",
            "resource_budget": {"cpu_cores": 4, "memory_gb": 8.0},
            "db_type": "postgres",
            "target": str(target),
            "sysbench_profile": {},
            # 1 screen row + 2 compile rejections = 3 attempts.
            "validation_attempts": [{"name": "e1"}],
            "experiment_history": [{"name": "e1"}],
            "validation_attempt_count": 3,
            "staging_issues": [],
        }
    )
    manifest = finalize_node(ctx).output
    assert manifest["attempt_count"] == 3


def test_experiments_run_increments_on_screen_and_rejection():
    ctx = _FakeContext({"knobs_info": [], "max_set_knobs": 20})
    out = stage_nodes.compile_candidate(
        ctx, {"name": "mystery", "phase": "screen", "levels": [{"knob": "nope", "value": "1"}]}
    )
    assert isinstance(out, CompileRejection)
    assert ctx.state["experiments_run"] == 1
    assert ctx.state["validation_attempt_count"] == 1

    stage_nodes.screen_candidate(
        _FakeContext(ctx.state), _compiled(), validate_fn=_validate_with_attestation
    )
    assert ctx.state["experiments_run"] == 2
    assert ctx.state["validation_attempt_count"] == 2


def test_decision_merges_rejected_history_with_row_reasons():
    row = {
        "status": "FAIL",
        "confirmed": False,
        "mean_delta_pct": -1.0,
        "lcb_pct": -2.0,
        "reasons": ["row reason"],
        "paired": None,
    }
    ctx = _FakeContext(
        {
            "all_rows": [row],
            "candidates": [],
            "rejected_history": ["compile boom"],
            "last_failure": ["compile boom"],
        }
    )
    dec = stage_nodes.decision(ctx)
    assert "row reason" in dec.summary["reasons"]
    assert "compile boom" in dec.summary["reasons"]


def test_controller_rejection_detail_persisted_and_surfaced_in_decision():
    from src.knob_tuner.workflow import compile_candidate_node

    ctx = _FakeContext(
        {
            "validation_attempt_count": 0,
            "max_attempts": 5,
            "experiment_history": [],
            "knobs_info": [],
            "max_set_knobs": 20,
        }
    )
    rej = compile_candidate_node(
        ctx,
        {"name": "e9", "phase": "screen", "levels": [{"knob": "nope", "value": "1"}]},
    )
    assert isinstance(rej, CompileRejection)
    assert ctx.state["last_rejection"]["reason"] == rej.reason
    out = confirmation_controller_node(ctx, rej)
    assert out["rejection_reason"] == rej.reason
    assert out["rejection_errors"] == list(rej.errors)
    assert ctx.state["last_controller"]["route"] == "retry"
    assert ctx.state["controller_history"][0]["rejection_reason"] == rej.reason

    # The decision output consumes the persisted controller verdict.
    row = {
        "status": "FAIL",
        "confirmed": False,
        "mean_delta_pct": -1.0,
        "lcb_pct": -2.0,
        "reasons": ["regression"],
        "paired": None,
    }
    dctx = _FakeContext(
        {
            "all_rows": [row],
            "candidates": [],
            "last_controller": {
                "route": "done",
                "reason": "attempt_cap",
                "gate": "disagree",
            },
        }
    )
    dec = stage_nodes.decision(dctx)
    assert dec.summary["controller_route"] == "done"
    assert dec.summary["controller_reason"] == "attempt_cap"
    assert any("controller: done (attempt_cap)" in r for r in dec.summary["reasons"])


def test_manifest_carries_pending_restart_knobs_from_live_result(tmp_path: Path):
    target = tmp_path / "app"
    target.mkdir()
    (target / "main.py").write_text("print('hi')\n")
    state = {
        "run_id": "run-abc",
        "result_status": "PASS",
        "resource_budget": {"cpu_cores": 4, "memory_gb": 8.0},
        "db_type": "postgres",
        "db_version": "16.3",
        "target": str(target),
        "sysbench_profile": {"threads": 6, "seed": 99},
        "validation_attempts": [{"name": "e1"}],
        "validation_attempt_count": 1,
        "applied_knobs": [{"knob": "shared_buffers"}],
        "staging_issues": [],
        "live_result": {
            "status": "APPLIED",
            "reason": "",
            "pending_restart_knobs": [{"name": "shared_buffers"}],
        },
    }
    assert finalize_node(_FakeContext(dict(state))).output[
        "pending_restart_knobs"
    ] == [{"name": "shared_buffers"}]
    assert _manifest_from_state(state).model_dump()[
        "pending_restart_knobs"
    ] == [{"name": "shared_buffers"}]


def test_builders_agree_on_status_errors_image_and_counts(tmp_path: Path):
    target = tmp_path / "app"
    target.mkdir()
    (target / "main.py").write_text("print('hi')\n")
    state = {
        "run_id": "run-abc",
        "result_status": "PASS",
        "resource_budget": {"cpu_cores": 4, "memory_gb": 8.0},
        "db_type": "postgres",
        "db_version": "16.3",
        "target": str(target),
        "sysbench_profile": {"threads": 6, "seed": 99},
        "validation_attempts": [{"name": "e1"}],
        "validation_attempt_count": 2,
        "staging_issues": ["row reason"],
        "validation_attestation": {"verified_knobs": [{"knob": "work_mem"}]},
        "validation_timings": {"baseline_seconds": 1.0},
        "live_result": {
            "status": "APPLIED",
            "reason": "",
            "pending_restart_knobs": [{"name": "shared_buffers"}],
        },
    }
    graph_manifest = finalize_node(_FakeContext(dict(state))).output
    fallback_manifest = _manifest_from_state(dict(state)).model_dump()
    for key in (
        "status",
        "errors",
        "db_image",
        "attempt_count",
        "verified_knobs",
        "pending_restart_knobs",
        "validation_timings",
        "final_status",
    ):
        assert graph_manifest[key] == fallback_manifest[key], key


def test_staging_issues_structured_with_attempt_and_knobs_and_deduped():
    structured = stage_nodes.structure_staging_issues(
        ["boom", "boom", "ok"], attempt=2, knob_names=["work_mem"]
    )
    assert structured == [
        "[attempt 2][knobs work_mem] boom",
        "[attempt 2][knobs work_mem] ok",
    ]

    row = {
        "status": "FAIL",
        "confirmed": False,
        "mean_delta_pct": -1.0,
        "lcb_pct": -2.0,
        "reasons": ["regression", "regression"],
        "paired": None,
    }
    ctx = _FakeContext(
        {
            "all_rows": [row],
            "candidates": [],
            "experiment_history": [],
            "rejected_history": [],
            "validation_attempt_count": 2,
        }
    )
    decision_node(ctx)
    issues = ctx.state["staging_issues"]
    assert len(issues) == len(set(issues))
    assert all(issue.startswith("[attempt 2]") for issue in issues)
    assert any("regression" in issue for issue in issues)


def test_crash_still_leaves_manifest_and_result(tmp_path: Path):
    target_dir = tmp_path / "target_app"
    target_dir.mkdir()
    results_dir = tmp_path / "results"

    with patch("src.knob_tuner.main.Runner") as mock_runner_cls, patch(
        "src.knob_tuner.main._process_cleanup"
    ):
        mock_runner = MagicMock()
        mock_runner.run_async.side_effect = RuntimeError("Runner crashed")
        mock_runner_cls.return_value = mock_runner
        try:
            asyncio.run(
                run_pipeline(
                    target=str(target_dir),
                    db_name="custom_db",
                    cpu_cores_arg=2,
                    memory_arg=4.0,
                    log_file=str(tmp_path / "log.log"),
                    results_dir=str(results_dir),
                    dry_run=True,
                )
            )
        except RuntimeError as exc:
            assert "Runner crashed" in str(exc)
        else:
            raise AssertionError("run_pipeline must re-raise the crash")

    run_dirs = [p for p in results_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((run_dirs[0] / "result.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "FAIL"
    assert any("Runner crashed" in err for err in manifest["errors"])
    assert result["status"] == "FAIL"
    assert os.path.isfile(run_dirs[0] / "result.json")
