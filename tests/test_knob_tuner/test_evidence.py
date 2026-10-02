"""Wave 1 closed-loop evidence tests: bundle + belief table."""

from src.knob_tuner.stages.evidence import MAX_BUNDLE_LINES, build_evidence_bundle
from src.knob_tuner.stages.models import KnobBelief, render_belief_table


def _row(i: int) -> dict:
    return {
        "name": f"exp_{i}",
        "phase": "screen",
        "n_knobs": 3,
        "mean_delta_pct": float(i),
        "lcb_pct": float(i) - 1.0,
        "status": "FAIL",
        "confirmed": False,
    }


def _state(**overrides) -> dict:
    state = {
        "experiment_history": [_row(1), _row(2)],
        "last_screen_row": {
            "arm": "exp_2",
            "phase": "screen",
            "mean_delta_pct": 2.0,
            "lcb_pct": 1.0,
            "ucb_pct": 3.0,
            "df": 4.0,
            "status": "FAIL",
            "confirmed": False,
            "reasons": ["no improvement"],
        },
        "rejected_history": ["no improvement"],
        "resource_budget": {"cpu": 4, "memory_gb": 8},
        "validation_attempt_count": 2,
        "max_attempts": 6,
    }
    state.update(overrides)
    return state


def test_bundle_table_correctness():
    text = build_evidence_bundle(_state())
    assert "## Evidence bundle" in text
    assert "Attempt 2/6" in text
    assert "cpu=4" in text and "8GB" in text
    assert "exp_1" in text and "exp_2" in text
    assert "Last verdict" in text
    assert "+2.00%" in text  # last-verdict mean
    assert "no improvement" in text


def test_bundle_resource_line_prefers_cpu_cores():
    text = build_evidence_bundle(
        _state(resource_budget={"cpu_cores": 16, "memory_gb": 8})
    )
    assert "cpu=16" in text
    # Legacy keys still render as a fallback.
    text_legacy = build_evidence_bundle(
        _state(resource_budget={"cpu": 4, "memory_gb": 8})
    )
    assert "cpu=4" in text_legacy


def test_bundle_missing_measurement_renders_na_not_zero():
    state = _state(
        experiment_history=[
            {
                "name": "exp_x",
                "phase": "screen",
                "n_knobs": 1,
                "mean_delta_pct": None,
                "lcb_pct": None,
                "status": "FAIL",
                "confirmed": False,
            }
        ],
        last_screen_row={
            "arm": "exp_x",
            "phase": "screen",
            "status": "FAIL",
            "confirmed": False,
            "reasons": ["boom"],
        },
    )
    text = build_evidence_bundle(state)
    assert "n/a" in text
    assert "+0.00%" not in text


def test_bundle_truncation_cap():
    state = _state(experiment_history=[_row(i) for i in range(60)])
    text = build_evidence_bundle(state)
    assert len(text.splitlines()) <= MAX_BUNDLE_LINES
    # Newest rows survive; oldest are truncated first.
    assert "exp_59" in text
    assert "omitted" in text


def test_bundle_empty_state_safe():
    text = build_evidence_bundle({})
    assert isinstance(text, str) and text
    assert len(text.splitlines()) <= MAX_BUNDLE_LINES
    assert "Attempt" in text
    text_none = build_evidence_bundle(None)  # type: ignore[arg-type]
    assert isinstance(text_none, str) and text_none


def test_belief_table_sorting():
    beliefs = {
        "b_knob": KnobBelief(best_delta_pct=1.0, n_seen=2, last_phase="screen"),
        "a_knob": KnobBelief(best_delta_pct=5.0, n_seen=1, last_phase="refinement"),
    }
    text = render_belief_table(beliefs)
    assert text.index("a_knob") < text.index("b_knob")
    assert "+5.00%" in text


def test_belief_table_cap_and_empty():
    beliefs = {f"k_{i:02d}": KnobBelief(best_delta_pct=float(i)) for i in range(30)}
    text = render_belief_table(beliefs)
    data_rows = [ln for ln in text.splitlines() if ln.startswith("| k_")]
    assert len(data_rows) == 20
    assert "omitted" in text
    assert render_belief_table({}) == "No knob beliefs yet."


# --- compounding campaign signals (Part 2) ---


def test_bundle_shows_winners_progress_line():
    text = build_evidence_bundle(
        _state(
            winners_found=3,
            success_candidates_target=10,
            min_improvement_pct_state=5.0,
        )
    )
    assert "Winners 3/10" in text
    assert "lcb >= 5.0%" in text


def test_belief_table_has_cleared_column():
    beliefs = {
        "work_mem": {"best_delta_pct": 12.0, "n_seen": 2, "last_phase": "screen",
                      "cleared": True},
        "shared_buffers": {"best_delta_pct": 1.0, "n_seen": 1,
                           "last_phase": "screen", "cleared": False},
    }
    text = render_belief_table(beliefs)
    assert "cleared" in text
    assert "yes" in text and "no" in text


def test_belief_model_cleared_defaults_false():
    assert KnobBelief().cleared is False
    assert KnobBelief(cleared=True).cleared is True
