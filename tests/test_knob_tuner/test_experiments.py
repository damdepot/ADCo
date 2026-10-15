"""Tests for the deterministic experiment-protocol core."""

import pytest
from pydantic import ValidationError

from src.knob_tuner.tools.experiments import (
    ExperimentArm,
    ExperimentLevel,
    ExperimentProtocol,
    expand_protocol,
    pick_winner,
    summarize_arm_results,
)


def _levels(prefix: str, count: int, start: int = 0) -> list[ExperimentLevel]:
    return [
        ExperimentLevel(knob=f"{prefix}_{i:02d}", value=str(100 + i))
        for i in range(start, start + count)
    ]


def _big_protocol() -> ExperimentProtocol:
    return ExperimentProtocol(
        objective="find best knobs",
        arms=[
            ExperimentArm(name="s1", phase="screen", levels=_levels("knob", 10, 0)),
            ExperimentArm(
                name="i1", phase="interaction", levels=_levels("knob", 8, 10)
            ),
            ExperimentArm(
                name="r1", phase="refinement", levels=_levels("knob", 6, 18)
            ),
        ],
    )


def test_phase_validation_rejects_bad_phase() -> None:
    with pytest.raises(ValidationError):
        ExperimentArm(
            name="bad",
            phase="exploit",
            levels=[ExperimentLevel(knob="a", value="1")],
        )


def test_phase_validation_normalizes_case_and_whitespace() -> None:
    arm = ExperimentArm(
        name="x",
        phase="  Screen ",
        levels=[ExperimentLevel(knob="a", value="1")],
    )
    assert arm.phase == "screen"


def test_duplicate_knob_in_arm_rejected() -> None:
    protocol = ExperimentProtocol(
        arms=[
            ExperimentArm(
                name="dup",
                phase="screen",
                levels=[
                    ExperimentLevel(knob="shared_buffers", value="1GB"),
                    ExperimentLevel(knob="shared_buffers", value="2GB"),
                ],
            )
        ]
    )
    plans, reasons = expand_protocol(protocol, {}, min_distinct_knobs=1)
    assert plans == []
    assert any("duplicate" in reason.lower() for reason in reasons)


def test_over_cap_protocol_rejected() -> None:
    arms = [
        ExperimentArm(name=f"s{i}", phase="screen", levels=_levels(f"cap{i}", 2))
        for i in range(13)
    ]
    protocol = ExperimentProtocol(arms=arms)
    plans, reasons = expand_protocol(protocol, {}, min_distinct_knobs=1)
    assert plans == []
    assert reasons


def test_distinct_knob_floor_enforced() -> None:
    protocol = ExperimentProtocol(
        arms=[
            ExperimentArm(name="small", phase="screen", levels=_levels("tiny", 2))
        ]
    )
    plans, reasons = expand_protocol(protocol, {})
    assert plans == []
    assert any("distinct" in reason.lower() for reason in reasons)


def test_valid_three_phase_protocol_expands() -> None:
    plans, reasons = expand_protocol(_big_protocol(), {})
    assert reasons == []
    assert len(plans) == 3
    assert [(phase, name) for (_, phase, name) in plans] == [
        ("screen", "s1"),
        ("interaction", "i1"),
        ("refinement", "r1"),
    ]
    assert [len(plan.knobs) for (plan, _, _) in plans] == [10, 8, 6]


def _rows() -> list[dict]:
    return [
        {
            "arm": "s1",
            "phase": "screen",
            "mean_delta_pct": 5.0,
            "lcb_pct": 1.0,
            "ucb_pct": 9.0,
            "status": "PASS",
            "reps": 3,
            "stopped_early": False,
        },
        {
            "arm": "i1",
            "phase": "interaction",
            "mean_delta_pct": 8.0,
            "lcb_pct": 2.0,
            "ucb_pct": 14.0,
            "status": "PASS",
            "reps": 3,
            "stopped_early": False,
        },
        {
            "arm": "r1",
            "phase": "refinement",
            "mean_delta_pct": 50.0,
            "lcb_pct": 40.0,
            "ucb_pct": 60.0,
            "status": "INCONCLUSIVE",
            "reps": 3,
            "stopped_early": True,
        },
        {
            "arm": "f1",
            "phase": "screen",
            "mean_delta_pct": 99.0,
            "lcb_pct": 90.0,
            "ucb_pct": 110.0,
            "status": "FAIL",
            "reps": 3,
            "stopped_early": False,
        },
    ]


def test_pick_winner_selects_best_pass() -> None:
    winner = pick_winner(_rows())
    assert winner is not None
    assert winner["arm"] == "i1"


def test_pick_winner_tie_breaks_on_lcb() -> None:
    rows = [
        {"arm": "a", "status": "PASS", "mean_delta_pct": 5.0, "lcb_pct": 1.0},
        {"arm": "b", "status": "PASS", "mean_delta_pct": 5.0, "lcb_pct": 3.0},
    ]
    winner = pick_winner(rows)
    assert winner is not None
    assert winner["arm"] == "b"


def test_pick_winner_none_without_pass() -> None:
    rows = [{"arm": "a", "status": "FAIL", "mean_delta_pct": 1.0, "lcb_pct": 0.0}]
    assert pick_winner(rows) is None
    assert pick_winner([]) is None


def test_summarize_contains_arm_names() -> None:
    summary = summarize_arm_results(_rows())
    for name in ("s1", "i1", "r1", "f1"):
        assert name in summary
    assert "Best arm: i1" in summary


# --- run_experiment_arms + format_protocol_feedback (appended) ---

from src.knob_tuner.contracts import KnobPlan, KnobSpec
from src.knob_tuner.tools.experiments import (
    format_protocol_feedback,
    run_experiment_arms,
)


def _plan(tag: str) -> KnobPlan:
    return KnobPlan(knobs=[KnobSpec(name=f"knob_{tag}", value="1")])


def _canned(status: str, base: list[float], tuned: list[float], **extra) -> dict:
    payload = {
        "status": status,
        "paired": {
            "baseline": {"per_run_tps": list(base)},
            "tuned": {"per_run_tps": list(tuned)},
        },
        "reasons": [],
        "stopped_early": False,
    }
    payload.update(extra)
    return payload


def test_run_arms_confirms_pass_non_worse() -> None:
    calls: list[dict] = []

    def fake_validate(*, plan, run_profile, shared_baseline, attempt, early_stop_min_reps):
        calls.append({"attempt": attempt, "early": early_stop_min_reps})
        return _canned("PASS", [100.0, 102.0, 101.0], [110.0, 112.0, 111.0])

    rows = run_experiment_arms(
        arms=[(_plan("a"), "screen", "arm_a")],
        shared_baseline=None,
        validate_fn=fake_validate,
        run_profile=object(),
        attempt_base=5,
        early_stop_min_reps=4,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["arm"] == "arm_a"
    assert row["phase"] == "screen"
    assert row["status"] == "PASS"
    assert row["confirmed"] is True
    assert row["mean_delta_pct"] > 0
    assert row["df"] >= 1
    assert row["reps"] == 3
    assert row["plan_hash"] == _plan("a").plan_hash()
    assert calls[0]["attempt"] == 5
    assert calls[0]["early"] == 4


def test_run_arms_rejects_negative_mean() -> None:
    def fake_validate(*, plan, run_profile, shared_baseline, attempt, early_stop_min_reps):
        return _canned("PASS", [100.0, 102.0, 101.0], [90.0, 91.0, 89.0])

    rows = run_experiment_arms(
        arms=[(_plan("b"), "screen", "arm_b")],
        shared_baseline=None,
        validate_fn=fake_validate,
        run_profile=object(),
        attempt_base=1,
        early_stop_min_reps=None,
    )
    assert rows[0]["confirmed"] is False
    assert rows[0]["mean_delta_pct"] < 0


def test_run_arms_error_row_and_loop_continues() -> None:
    def fake_validate(*, plan, run_profile, shared_baseline, attempt, early_stop_min_reps):
        if attempt == 11:
            raise RuntimeError("boom")
        return _canned("PASS", [100.0, 102.0, 101.0], [110.0, 112.0, 111.0])

    messages: list[str] = []
    rows = run_experiment_arms(
        arms=[
            (_plan("c"), "screen", "bad_arm"),
            (_plan("d"), "screen", "good_arm"),
        ],
        shared_baseline=None,
        validate_fn=fake_validate,
        run_profile=object(),
        attempt_base=11,
        early_stop_min_reps=3,
        progress=messages.append,
    )
    assert len(rows) == 2
    assert rows[0]["status"] == "ERROR"
    assert rows[0]["confirmed"] is False
    assert "boom" in rows[0]["reasons"][0]
    assert rows[1]["status"] == "PASS"
    assert rows[1]["confirmed"] is True
    assert messages  # progress callback was exercised


def test_run_arms_attempt_numbering_and_early_stop_forwarded() -> None:
    seen: list[tuple[int, object]] = []

    def fake_validate(*, plan, run_profile, shared_baseline, attempt, early_stop_min_reps):
        seen.append((attempt, early_stop_min_reps))
        return _canned("FAIL", [100.0, 101.0], [100.5, 101.5])

    run_experiment_arms(
        arms=[(_plan("e"), "screen", "a1"), (_plan("f"), "screen", "a2")],
        shared_baseline="sb",
        validate_fn=fake_validate,
        run_profile="prof",
        attempt_base=7,
        early_stop_min_reps=6,
    )
    assert [attempt for attempt, _ in seen] == [7, 8]
    assert all(early == 6 for _, early in seen)


def test_run_arms_winner_and_summarize_integration() -> None:
    def fake_validate(*, plan, run_profile, shared_baseline, attempt, early_stop_min_reps):
        if attempt == 1:
            return _canned("PASS", [100.0, 102.0, 101.0], [105.0, 106.0, 107.0])
        return _canned("PASS", [100.0, 102.0, 101.0], [120.0, 121.0, 122.0])

    rows = run_experiment_arms(
        arms=[(_plan("g"), "screen", "low"), (_plan("h"), "screen", "high")],
        shared_baseline=None,
        validate_fn=fake_validate,
        run_profile=object(),
        attempt_base=1,
        early_stop_min_reps=None,
    )
    winner = pick_winner(rows)
    assert winner is not None
    assert winner["arm"] == "high"
    summary = summarize_arm_results(rows)
    assert "low" in summary and "high" in summary
    assert "Best arm: high" in summary


def test_format_protocol_feedback_contains_names_and_verdicts() -> None:
    rows = [
        {"arm": "arm_x", "phase": "screen", "mean_delta_pct": 5.0,
         "lcb_pct": 3.0, "ucb_pct": 7.0, "status": "PASS",
         "reps": 3, "confirmed": True},
        {"arm": "arm_y", "phase": "screen", "mean_delta_pct": -2.0,
         "lcb_pct": -4.0, "ucb_pct": 0.0, "status": "FAIL",
         "reps": 3, "confirmed": False},
    ]
    text = format_protocol_feedback("retry-1", rows)
    assert "arm_x" in text and "arm_y" in text
    assert "CONFIRMED" in text
    assert "rejected" in text
    assert "do not repeat" in text.lower()
