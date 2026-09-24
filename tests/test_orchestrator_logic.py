"""Tests for rewriter orchestration logic (no live agents)."""

import asyncio
import inspect

import pytest
from google.adk import Event
from pydantic import ValidationError

from src.code_rewriter.workflow import (
    MAX_ATTEMPTS_PER_TARGET,
    _attempt_score,
    _issue_signature,
    attempts_exhausted,
    finalize,
    make_orchestrate,
    prepare,
)
from src.code_rewriter.models.rewrite_models import RewriteContract, RewriteTarget
from src.code_rewriter.sub_agents.verifier.models import VerifierOutput
from src.code_rewriter.tools.pipeline_analysis import verify_all_contracts


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


def test_finalize_fails_when_no_targets():
    """An empty run must not vacuously PASS: zero targets is a FAIL."""
    ctx = _FakeContext({"target_results": []})
    event = finalize(ctx)
    delta = event.actions.state_delta
    assert delta["deterministic_verification"]["status"] == "FAIL"
    assert delta["verifier_output"]["status"] == "FAIL"
    assert delta["deterministic_verification"]["expected_targets"] == 0


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
    result = verify_all_contracts(
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


class _FakeAsyncContext:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state if state is not None else {}

    async def run_node(self, node, node_input=None):
        result = node(self, node_input)
        if inspect.isawaitable(result):
            result = await result
        return result


async def _drive(orchestrate, ctx):
    events = []
    async for event in orchestrate(ctx):
        events.append(event)
    return events


def test_make_orchestrate_restores_best_attempt(tmp_path):
    """The best-scoring attempt must win, not merely the last one."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    target_file = sandbox / "repo.py"
    target_file.write_text("original\n")

    markers = iter(["BEST", "WORSE", "WORSE"])

    def optimizer(ctx, node_input=None):
        target_file.write_text(next(markers) + "\n")

    first_verdict = {
        "status": "FAIL",
        "violations": [{"code": "DEAD_LOCAL", "severity": "ERROR"}],
    }
    worse_verdict = {
        "status": "FAIL",
        "violations": [
            {"code": "STRATEGY_NOT_APPLIED", "severity": "ERROR"},
            {"code": "N_PLUS_ONE_QUERY", "severity": "ERROR"},
        ],
    }
    all_verdicts = iter([first_verdict, worse_verdict, worse_verdict])

    def verify(ctx, node_input=None):
        return next(all_verdicts)

    ctx = _FakeAsyncContext({
        "sandbox": str(sandbox),
        "rewrite_contracts": [
            {"target": {"file": "repo.py", "qualified_function": "get_users"}}
        ],
    })
    orchestrate = make_orchestrate(optimizer, verify)
    events = asyncio.run(_drive(orchestrate, ctx))

    assert target_file.read_text() == "BEST\n"
    assert _attempt_score(first_verdict) < _attempt_score(worse_verdict)
    results = events[-1].actions.state_delta["target_results"]
    assert results[0]["status"] == "FAIL"
    assert results[0]["verification"] == first_verdict


def test_issue_signature_is_stable_and_sorted():
    """Signature is order-independent, stable, and empty when there are none."""
    assert _issue_signature(None) == ()
    assert _issue_signature({"violations": []}) == ()
    a = {
        "violations": [
            {"code": "B", "severity": "ERROR", "message": "second"},
            {"code": "A", "severity": "WARNING", "message": "first"},
        ]
    }
    b = {
        "violations": [
            {"code": "A", "severity": "WARNING", "message": "first"},
            {"code": "B", "severity": "ERROR", "message": "second"},
        ]
    }
    assert _issue_signature(a) == _issue_signature(b)
    assert _issue_signature(a) == (
        ("ERROR", "B", "second"),
        ("WARNING", "A", "first"),
    )


def test_make_orchestrate_repairs_from_best(tmp_path):
    """Each retry must repair the best attempt, and the best artifact wins."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    target_file = sandbox / "repo.py"
    target_file.write_text("original\n")

    markers = iter(["BEST", "MID", "LAST"])
    observed: list[str] = []

    def optimizer(ctx, node_input=None):
        observed.append(target_file.read_text())
        target_file.write_text(next(markers) + "\n")

    best_verdict = {
        "status": "FAIL",
        "violations": [{"code": "A", "severity": "ERROR", "message": "a"}],
    }
    mid_verdict = {
        "status": "FAIL",
        "violations": [
            {"code": "A", "severity": "ERROR", "message": "a"},
            {"code": "B", "severity": "ERROR", "message": "b"},
        ],
    }
    last_verdict = {
        "status": "FAIL",
        "violations": [
            {"code": "A", "severity": "ERROR", "message": "a"},
            {"code": "B", "severity": "ERROR", "message": "b"},
            {"code": "C", "severity": "ERROR", "message": "c"},
        ],
    }
    verdicts = iter([best_verdict, mid_verdict, last_verdict])

    def verify(ctx, node_input=None):
        return next(verdicts)

    ctx = _FakeAsyncContext({
        "sandbox": str(sandbox),
        "rewrite_contracts": [
            {"target": {"file": "repo.py", "qualified_function": "get_users"}}
        ],
    })
    orchestrate = make_orchestrate(optimizer, verify, max_attempts=3)
    events = asyncio.run(_drive(orchestrate, ctx))

    # Attempt 2 and 3 must both observe the best attempt's artifact as their base.
    assert observed == ["original\n", "BEST\n", "BEST\n"]
    assert target_file.read_text() == "BEST\n"
    results = events[-1].actions.state_delta["target_results"]
    assert results[0]["status"] == "FAIL"
    assert results[0]["verification"] == best_verdict


def test_make_orchestrate_stops_on_repeated_signature(tmp_path):
    """A repeated violation signature halts the loop (no-progress detection)."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    target_file = sandbox / "repo.py"
    target_file.write_text("original\n")

    optimizer_calls = {"n": 0}

    def optimizer(ctx, node_input=None):
        optimizer_calls["n"] += 1
        target_file.write_text("attempt\n")

    repeated_verdict = {
        "status": "FAIL",
        "violations": [{"code": "STRATEGY_NOT_APPLIED", "severity": "ERROR"}],
    }

    def verify(ctx, node_input=None):
        return repeated_verdict

    ctx = _FakeAsyncContext({
        "sandbox": str(sandbox),
        "rewrite_contracts": [
            {"target": {"file": "repo.py", "qualified_function": "get_users"}}
        ],
    })
    orchestrate = make_orchestrate(optimizer, verify)
    events = asyncio.run(_drive(orchestrate, ctx))

    assert optimizer_calls["n"] == 2
    results = events[-1].actions.state_delta["target_results"]
    assert results[0]["status"] == "FAIL"


def test_make_orchestrate_max_attempts_is_five(tmp_path):
    """Never-passing distinct failures exhaust the five-attempt budget."""
    assert MAX_ATTEMPTS_PER_TARGET == 5

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    target_file = sandbox / "repo.py"
    target_file.write_text("original\n")

    optimizer_calls = {"n": 0}

    def optimizer(ctx, node_input=None):
        optimizer_calls["n"] += 1
        target_file.write_text(f"attempt-{optimizer_calls['n']}\n")

    verify_calls = {"n": 0}

    def verify(ctx, node_input=None):
        verify_calls["n"] += 1
        return {
            "status": "FAIL",
            "violations": [
                {
                    "code": f"E{verify_calls['n']}",
                    "severity": "ERROR",
                    "message": str(verify_calls["n"]),
                }
            ],
        }

    ctx = _FakeAsyncContext({
        "sandbox": str(sandbox),
        "rewrite_contracts": [
            {"target": {"file": "repo.py", "qualified_function": "get_users"}}
        ],
    })
    orchestrate = make_orchestrate(optimizer, verify)
    events = asyncio.run(_drive(orchestrate, ctx))

    assert optimizer_calls["n"] == 5
    results = events[-1].actions.state_delta["target_results"]
    assert results[0]["status"] == "FAIL"


def test_make_orchestrate_keeps_passing_attempt(tmp_path):
    """A passing attempt is left in place; no best-attempt restore happens."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    target_file = sandbox / "repo.py"
    target_file.write_text("original\n")

    markers = iter(["PASSING", "WORSE", "WORSE"])

    def optimizer(ctx, node_input=None):
        target_file.write_text(next(markers) + "\n")

    def verify(ctx, node_input=None):
        return {"status": "PASS", "violations": []}

    ctx = _FakeAsyncContext({
        "sandbox": str(sandbox),
        "rewrite_contracts": [
            {"target": {"file": "repo.py", "qualified_function": "get_users"}}
        ],
    })
    orchestrate = make_orchestrate(optimizer, verify)
    events = asyncio.run(_drive(orchestrate, ctx))

    assert target_file.read_text() == "PASSING\n"
    results = events[-1].actions.state_delta["target_results"]
    assert results[0]["status"] == "PASS"


def test_make_orchestrate_llm_fail_with_evidence_retries(tmp_path):
    """An evidence-backed LLM FAIL after deterministic PASS drives a retry."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    target_file = sandbox / "repo.py"
    target_file.write_text("original\n")

    optimizer_calls = {"n": 0}

    def optimizer(ctx, node_input=None):
        optimizer_calls["n"] += 1
        target_file.write_text(f"attempt-{optimizer_calls['n']}\n")

    def verify(ctx, node_input=None):
        return {"status": "PASS", "violations": []}

    llm_calls = {"n": 0}

    def llm_verify(ctx, node_input=None):
        llm_calls["n"] += 1
        if llm_calls["n"] == 1:
            return {
                "status": "FAIL",
                "issues": [
                    {
                        "code": "SEMANTIC_ISSUE",
                        "severity": "ERROR",
                        "message": "wrong join",
                        "evidence": "line 42: JOIN ...",
                    }
                ],
            }
        return {"status": "PASS", "issues": []}

    ctx = _FakeAsyncContext({
        "sandbox": str(sandbox),
        "rewrite_contracts": [
            {"target": {"file": "repo.py", "qualified_function": "get_users"}}
        ],
    })
    orchestrate = make_orchestrate(optimizer, verify, llm_verify, max_attempts=3)
    events = asyncio.run(_drive(orchestrate, ctx))

    # The merged evidence-backed FAIL must not pass on attempt 1: optimizer reruns.
    assert optimizer_calls["n"] == 2
    results = events[-1].actions.state_delta["target_results"]
    assert results[0]["status"] == "PASS"


def _restricted_run(tmp_path, risk_rejections):
    """Drive make_orchestrate with an optimizer that writes nothing."""
    target_dir = tmp_path / "target"
    sandbox = tmp_path / "sandbox"
    target_dir.mkdir()
    sandbox.mkdir()
    code = "def get_users(ids):\n    return [fetch(x) for x in ids]\n"
    (target_dir / "repo.py").write_text(code)
    (sandbox / "repo.py").write_text(code)

    def optimizer(ctx, node_input=None):
        pass

    def verify(ctx, node_input=None):
        return {
            "status": "FAIL",
            "violations": [
                {"code": "MISSING_REWRITE", "severity": "ERROR", "message": "unchanged"}
            ],
        }

    state = {
        "target": str(target_dir),
        "sandbox": str(sandbox),
        "rewrite_contracts": [
            {"target": {"file": "repo.py", "qualified_function": "get_users"}}
        ],
    }
    if risk_rejections is not None:
        state["risk_rejections"] = risk_rejections
    ctx = _FakeAsyncContext(state)
    orchestrate = make_orchestrate(optimizer, verify, max_attempts=2)
    events = asyncio.run(_drive(orchestrate, ctx))
    return ctx, events


def test_make_orchestrate_restricted_when_high_risk_blocked(tmp_path):
    """A blocked HIGH-risk target whose original survives becomes RESTRICTED."""
    ctx, events = _restricted_run(tmp_path, ["get_users"])
    results = events[-1].actions.state_delta["target_results"]
    assert results[0]["status"] == "RESTRICTED"
    assert results[0]["verification"]["status"] == "RESTRICTED"

    ctx.state["target_results"] = results
    event = finalize(ctx)
    det = event.actions.state_delta["deterministic_verification"]
    assert det["status"] == "PASS"
    assert det["restricted_targets"] == 1
    assert det["transformed_targets"] == 0
    vo = event.actions.state_delta["verifier_output"]
    assert vo["status"] == "PASS"
    assert "[RESTRICTED] repo.py::get_users" in vo["detail"]


def test_make_orchestrate_unchanged_target_becomes_restricted(tmp_path):
    """An unchanged target with no new attempt is RESTRICTED, not FAIL."""
    _, events = _restricted_run(tmp_path, None)
    results = events[-1].actions.state_delta["target_results"]
    assert results[0]["status"] == "RESTRICTED"
    assert results[0]["verification"]["status"] == "RESTRICTED"


def test_make_orchestrate_records_no_attempt_when_optimizer_writes_nothing(tmp_path):
    """A finished optimizer run that leaves the target unchanged records NO_ATTEMPT."""
    ctx, _ = _restricted_run(tmp_path, None)
    attempts = ctx.state["optimizer_attempts"]
    assert any(
        entry.get("outcome") == "NO_ATTEMPT"
        and entry.get("codes") == ["NO_REWRITE"]
        and entry.get("function") == "get_users"
        for entry in attempts
    )


def test_make_orchestrate_restricted_no_rewrite_is_finalize_pass(tmp_path):
    """An unchanged target restricted by the orchestrator finalizes as a PASS."""
    ctx, events = _restricted_run(tmp_path, None)
    results = events[-1].actions.state_delta["target_results"]
    ctx.state["target_results"] = results

    event = finalize(ctx)
    det = event.actions.state_delta["deterministic_verification"]
    assert det["status"] == "PASS"
    assert det["restricted_targets"] == 1


def test_make_orchestrate_rejected_but_modified_stays_fail(tmp_path):
    """RESTRICTED requires the original to be intact; a kept rewrite stays FAIL."""
    target_dir = tmp_path / "target"
    sandbox = tmp_path / "sandbox"
    target_dir.mkdir()
    sandbox.mkdir()
    code = "def get_users(ids):\n    return [fetch(x) for x in ids]\n"
    (target_dir / "repo.py").write_text(code)
    (sandbox / "repo.py").write_text("def get_users(ids):\n    return fetch_all(ids)\n")

    def optimizer(ctx, node_input=None):
        pass

    def verify(ctx, node_input=None):
        return {
            "status": "FAIL",
            "violations": [
                {"code": "MISSING_REWRITE", "severity": "ERROR", "message": "bad"}
            ],
        }

    ctx = _FakeAsyncContext({
        "target": str(target_dir),
        "sandbox": str(sandbox),
        "rewrite_contracts": [
            {"target": {"file": "repo.py", "qualified_function": "get_users"}}
        ],
        "risk_rejections": ["get_users"],
    })
    orchestrate = make_orchestrate(optimizer, verify, max_attempts=1)
    events = asyncio.run(_drive(orchestrate, ctx))
    results = events[-1].actions.state_delta["target_results"]
    assert results[0]["status"] == "FAIL"


def test_finalize_ordinary_fail_still_fails():
    """A RESTRICTED-aware finalize must not accept an ordinary FAIL."""
    ctx = _FakeContext({
        "target_results": [
            {
                "file": "repo.py",
                "function": "get_users",
                "status": "FAIL",
                "verification": {
                    "status": "FAIL",
                    "summary": "residual loop",
                    "violations": [
                        {
                            "code": "STRATEGY_NOT_APPLIED",
                            "severity": "ERROR",
                            "message": "loop remains",
                        }
                    ],
                },
            }
        ],
        "verifier_output": {"status": "PASS", "category": "NONE"},
    })
    event = finalize(ctx)
    delta = event.actions.state_delta
    assert delta["deterministic_verification"]["status"] == "FAIL"
    assert delta["deterministic_verification"]["restricted_targets"] == 0


def test_make_orchestrate_llm_fail_without_evidence_is_ignored(tmp_path):
    """An unevidenced LLM FAIL must not block the deterministic PASS."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    target_file = sandbox / "repo.py"
    target_file.write_text("original\n")

    optimizer_calls = {"n": 0}

    def optimizer(ctx, node_input=None):
        optimizer_calls["n"] += 1
        target_file.write_text("attempt\n")

    def verify(ctx, node_input=None):
        return {"status": "PASS", "violations": []}

    def llm_verify(ctx, node_input=None):
        return {
            "status": "FAIL",
            "issues": [
                {
                    "code": "SEMANTIC_ISSUE",
                    "severity": "ERROR",
                    "message": "unproven claim",
                    "evidence": "",
                }
            ],
        }

    ctx = _FakeAsyncContext({
        "sandbox": str(sandbox),
        "rewrite_contracts": [
            {"target": {"file": "repo.py", "qualified_function": "get_users"}}
        ],
    })
    orchestrate = make_orchestrate(optimizer, verify, llm_verify, max_attempts=3)
    events = asyncio.run(_drive(orchestrate, ctx))

    assert optimizer_calls["n"] == 1
    results = events[-1].actions.state_delta["target_results"]
    assert results[0]["status"] == "PASS"

