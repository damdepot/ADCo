"""Tests for the structured optimizer repair-request models."""

from src.code_rewriter.models.feedback_models import (
    OptimizerAttempt,
    RepairIssue,
    build_repair_issues,
    count_optimizer_attempts,
    record_optimizer_attempt,
    render_optimizer_attempts,
    render_repair_request,
)


def _verification():
    return {
        "status": "FAIL",
        "violations": [
            {
                "code": "STRATEGY_NOT_APPLIED",
                "severity": "ERROR",
                "message": "2 DB ops remain inside loops",
                "expected": 0,
                "actual": 2,
            },
            {
                "code": "DUPLICATE_WHERE",
                "severity": "ERROR",
                "message": "statement has 2 WHERE clauses",
            },
        ],
        "target_coverage": [
            {
                "file": "repo.py",
                "function": "process_orders",
                "status": "MISSING_REWRITE",
                "details": "1 loop op remains",
            },
            {
                "file": "repo.py",
                "function": "other",
                "status": "TRANSFORMED",
                "details": "done",
            },
        ],
    }


def test_build_repair_issues_maps_violations_and_coverage():
    issues = build_repair_issues(_verification())

    codes = [i.code for i in issues]
    assert codes.count("STRATEGY_NOT_APPLIED") == 1
    assert codes.count("DUPLICATE_WHERE") == 1
    assert codes.count("MISSING_REWRITE") == 1
    assert "TRANSFORMED" not in codes

    strategy = next(i for i in issues if i.code == "STRATEGY_NOT_APPLIED")
    assert strategy.severity == "ERROR"
    assert strategy.expected == 0
    assert strategy.actual == 2
    assert strategy.fix_hint

    coverage = next(i for i in issues if i.code == "MISSING_REWRITE")
    assert coverage.function == "process_orders"
    assert coverage.file == "repo.py"
    assert coverage.message == "1 loop op remains"

    duplicate = next(i for i in issues if i.code == "DUPLICATE_WHERE")
    assert "second WHERE" in duplicate.fix_hint


def test_build_repair_issues_handles_non_dict():
    assert build_repair_issues(None) == []
    assert build_repair_issues({}) == []


def test_render_repair_request_contains_codes_and_definition_of_done():
    rendered = render_repair_request(build_repair_issues(_verification()))

    assert "## Repair Request" in rendered
    assert "STRATEGY_NOT_APPLIED" in rendered
    assert "DUPLICATE_WHERE" in rendered
    assert "MISSING_REWRITE" in rendered
    assert "process_orders" in rendered
    assert rendered.count("Definition of done:") == 3


def test_render_repair_request_empty():
    rendered = render_repair_request([])
    assert "## Repair Request" in rendered
    assert "(no issues)" in rendered


def test_render_repair_request_steers_to_composite_key_batching():
    rendered = render_repair_request(build_repair_issues(_verification()))
    lowered = rendered.lower()
    assert "composite-key" in lowered or "in ((" in lowered


def test_repair_issue_defaults():
    issue = RepairIssue(code="X", severity="ERROR", message="m")
    assert issue.file == ""
    assert issue.function == ""
    assert issue.line is None
    assert issue.fix_hint == ""
    assert issue.evidence == ""


def test_build_repair_issues_reads_optional_location_and_evidence():
    issues = build_repair_issues(
        {
            "status": "FAIL",
            "violations": [
                {
                    "code": "SEMANTIC_ISSUE",
                    "severity": "ERROR",
                    "message": "wrong join",
                    "function": "get_users",
                    "file": "repo.py",
                    "line": 42,
                    "evidence": "line 42: JOIN ...",
                }
            ],
        }
    )
    issue = issues[0]
    assert issue.function == "get_users"
    assert issue.file == "repo.py"
    assert issue.line == 42
    assert issue.evidence == "line 42: JOIN ..."


def test_record_optimizer_attempt_appends_and_fills_bare_name():
    state = {}
    attempt = OptimizerAttempt(
        function="Db.doX",
        outcome="REJECTED",
        codes=["DUPLICATE_WHERE"],
        message="m",
        diff="a\nb",
    )

    record_optimizer_attempt(state, attempt)

    entries = state["optimizer_attempts"]
    assert len(entries) == 1
    assert isinstance(entries[0], dict)
    assert entries[0]["bare_function"] == "doX"
    assert entries[0]["codes"] == ["DUPLICATE_WHERE"]
    assert entries[0]["diff"] == "a\nb"


def test_record_optimizer_attempt_truncates_diff():
    state = {}
    attempt = OptimizerAttempt(
        function="doX",
        outcome="REJECTED",
        diff="\n".join(str(i) for i in range(100)),
    )

    record_optimizer_attempt(state, attempt)

    stored = state["optimizer_attempts"][0]["diff"]
    assert stored.endswith("... (diff truncated)")
    assert len(stored.splitlines()) <= 41


def test_record_optimizer_attempt_caps_per_target():
    state = {}
    for _ in range(15):
        record_optimizer_attempt(
            state, OptimizerAttempt(file="a.py", function="doX", outcome="REJECTED")
        )
    record_optimizer_attempt(
        state, OptimizerAttempt(file="b.py", function="doX", outcome="REJECTED")
    )

    entries = state["optimizer_attempts"]
    same_target = [
        e for e in entries if e.get("file") == "a.py" and e.get("bare_function") == "doX"
    ]
    other_target = [e for e in entries if e.get("file") == "b.py"]
    assert len(same_target) == 12
    assert len(other_target) == 1


def test_count_optimizer_attempts_filters_by_target():
    state = {}
    record_optimizer_attempt(
        state, OptimizerAttempt(file="a.py", function="Db.doX", outcome="APPLIED")
    )
    record_optimizer_attempt(
        state, OptimizerAttempt(file="a.py", function="Db.doY", outcome="APPLIED")
    )
    record_optimizer_attempt(
        state, OptimizerAttempt(file="b.py", function="Db.doX", outcome="APPLIED")
    )

    assert count_optimizer_attempts(state, "a.py", "doX") == 1
    assert count_optimizer_attempts(state, "b.py", "Db.doX") == 1
    assert count_optimizer_attempts({}, "a.py", "doX") == 0


def test_render_optimizer_attempts_empty():
    assert render_optimizer_attempts({}, "a.py", "doX") == ""
    state = {"optimizer_attempts": [{"file": "a.py", "bare_function": "other"}]}
    assert render_optimizer_attempts(state, "a.py", "doX") == ""


def test_render_optimizer_attempts_filters_and_orders():
    state = {}
    for number in (1, 2, 3):
        record_optimizer_attempt(
            state,
            OptimizerAttempt(
                file="a.py",
                function="doX",
                outcome="REJECTED",
                message=f"m{number}",
                attempt=number,
            ),
        )
    record_optimizer_attempt(
        state,
        OptimizerAttempt(
            file="b.py",
            function="doX",
            outcome="REJECTED",
            message="bmsg",
            attempt=1,
        ),
    )

    rendered = render_optimizer_attempts(state, "a.py", "doX", limit=2)

    assert "## Prior Optimizer Attempts" in rendered
    assert "bmsg" not in rendered
    assert rendered.index("attempt 3") < rendered.index("attempt 2")

    qualified = render_optimizer_attempts(state, "a.py", "Db.doX", limit=2)
    assert "## Prior Optimizer Attempts" in qualified
    assert "bmsg" not in qualified
