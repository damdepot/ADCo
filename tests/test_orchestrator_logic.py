"""Tests for rewriter orchestration logic (no live agents)."""

import pytest
from google.adk import Event
from pydantic import ValidationError

from src.code_rewriter.workflow import (
    MAX_ATTEMPTS_PER_TARGET,
    attempts_exhausted,
    finalize,
    prepare,
)
from src.code_rewriter.models.rewrite_models import RewriteContract, RewriteTarget
from src.code_rewriter.sub_agents.verifier.models import VerifierOutput
from src.code_rewriter.tools.pipeline_analysis import execute_deterministic_verification


class _FakeContext:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state if state is not None else {}


def test_verifier_output_category_enum_rejects_unknown():
    with pytest.raises(ValidationError):
        VerifierOutput(status="FAIL", category="no_change_applied")


def test_verifier_output_accepts_prompt_enum():
    out = VerifierOutput(status="FAIL", category="strategy_not_applied")
    assert out.category == "strategy_not_applied"


def test_attempts_exhausted():
    assert attempts_exhausted(MAX_ATTEMPTS_PER_TARGET) is True
    assert attempts_exhausted(MAX_ATTEMPTS_PER_TARGET + 1) is True
    assert attempts_exhausted(MAX_ATTEMPTS_PER_TARGET - 1) is False
    assert attempts_exhausted(0) is False


def test_prepare_seeds_state():
    contracts = [
        {"target": {"file": "a.py", "function": "f"}},
        {"target": {"file": "b.py", "function": "g"}},
    ]
    ctx = _FakeContext({"rewrite_contracts": contracts})
    event = prepare(ctx)
    assert isinstance(event, Event)
    delta = event.actions.state_delta
    assert delta["current_contract"] == contracts[0]
    assert delta["target_index"] == 0
    assert delta["target_results"] == []
    assert delta["last_failure"] is None


def test_finalize_forces_fail_over_llm_pass():
    ctx = _FakeContext({
        "target_results": [
            {
                "file": "a.py",
                "function": "f",
                "status": "FAIL",
                "verification": {
                    "status": "FAIL",
                    "summary": "residual loop",
                    "violations": [
                        {
                            "code": "STRATEGY_NOT_APPLIED",
                            "severity": "ERROR",
                            "message": "x",
                        }
                    ],
                },
            }
        ],
        "verifier_output": {"status": "PASS", "category": "NONE"},
    })
    event = finalize(ctx)
    delta = event.actions.state_delta
    assert delta["verifier_output"]["status"] == "FAIL"
    assert delta["deterministic_verification"]["status"] == "FAIL"


def test_finalize_passes_when_all_targets_pass():
    ctx = _FakeContext({
        "target_results": [
            {
                "file": "a.py",
                "function": "f",
                "status": "PASS",
                "verification": None,
            }
        ],
        "verifier_output": {"status": "PASS", "category": "NONE", "reason": "ok"},
    })
    event = finalize(ctx)
    delta = event.actions.state_delta
    assert delta["verifier_output"]["status"] == "PASS"
    assert delta["deterministic_verification"]["status"] == "PASS"
    assert delta["deterministic_verification"]["transformed_targets"] == 1


def test_finalize_surfaces_warnings_without_failing():
    """Advisory WARNINGs are surfaced but must not flip a deterministic PASS."""
    ctx = _FakeContext({
        "target_results": [
            {
                "file": "a.py",
                "function": "f",
                "status": "PASS",
                "verification": {
                    "status": "PASS",
                    "violations": [
                        {
                            "code": "DEAD_LOCAL",
                            "severity": "WARNING",
                            "message": "unused x",
                        }
                    ],
                },
            }
        ],
        "verifier_output": {"status": "PASS", "category": "NONE"},
    })
    event = finalize(ctx)
    delta = event.actions.state_delta
    assert delta["verifier_output"]["status"] == "PASS"
    assert delta["deterministic_verification"]["status"] == "PASS"
    warning_text = "[DEAD_LOCAL] unused x"
    assert warning_text in delta["verifier_output"]["detail"] or any(
        w.get("code") == "DEAD_LOCAL"
        for w in delta["deterministic_verification"]["violations"]
    )


def test_vacuous_pass_rejected_when_non_contract_file_modified_only(tmp_path):
    """Regression: modifying only a non-contract file must not yield PASS."""
    target_dir = tmp_path / "target"
    sandbox_dir = tmp_path / "sandbox"
    target_dir.mkdir()
    sandbox_dir.mkdir()
    (target_dir / "repo.py").write_text(
        "def get_users(ids):\n"
        "    for user_id in ids:\n"
        "        cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))\n"
    )
    (sandbox_dir / "unrelated.py").write_text("print('hello')\n")

    contracts = [
        RewriteContract(
            rewrite_id="c1",
            target=RewriteTarget(file="repo.py", function="get_users"),
            pattern="N_PLUS_ONE_QUERY",
            strategy="COMBINING_QUERIES",
            targets=[RewriteTarget(file="repo.py", function="get_users")],
        )
    ]
    result = execute_deterministic_verification(
        str(target_dir), str(sandbox_dir), contracts, ["unrelated.py"]
    )
    assert result.status == "FAIL"
    assert result.expected_targets == 1
    assert result.transformed_targets == 0
    assert result.missing_targets == 1
    assert result.rewrite_coverage == 0.0


def test_finalize_deterministic_pass_not_overridden_by_llm_fail():
    """Deterministic PASS is authoritative; an advisory LLM FAIL must not block it."""
    ctx = _FakeContext({
        "target_results": [
            {"file": "a.py", "function": "f", "status": "PASS", "verification": None}
        ],
        "verifier_output": {
            "status": "FAIL",
            "category": "strategy_not_applied",
            "reason": "hallucinated residual loop ops",
            "suggestion": "double-check batching",
        },
    })
    event = finalize(ctx)
    delta = event.actions.state_delta
    assert delta["verifier_output"]["status"] == "PASS"
    assert delta["deterministic_verification"]["status"] == "PASS"
    assert delta["verifier_output"]["suggestion"] == "double-check batching"
