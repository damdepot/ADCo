"""Tests for the structured optimizer repair-request models."""

from src.code_rewriter.models.feedback_models import (
    RepairIssue,
    build_repair_issues,
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
