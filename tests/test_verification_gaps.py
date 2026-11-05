"""Verification-gaps channel: skipped checks must surface as PARTIAL evidence."""

import json

from src.code_rewriter.main import _write_output_result
from src.code_rewriter.models.rewrite_models import RewriteContract, RewriteTarget
from src.code_rewriter.tools.contract_verifier import verify_contract
from src.code_rewriter.tools.pipeline_analysis import verify_all_contracts
from src.code_rewriter.workflow import finalize


class _Ctx:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state or {}


def _contract(function: str = "doDelivery", file: str = "t.py") -> RewriteContract:
    return RewriteContract(
        rewrite_id="gap-test",
        target=RewriteTarget(file=file, function=function),
        pattern="N+1 Query",
        strategy="Query Batching",
        allowed_regions=[function],
    )


UNBATCHED = '''
def doDelivery(cursor, ids, w_id, carrier):
    for o_id in ids:
        cursor.execute("UPDATE ORDERS SET O_CARRIER_ID = %s WHERE O_ID = %s", (carrier, o_id))
    return []
'''

DYNAMIC_BATCHED = '''
def doDelivery(cursor, ids, w_id, carrier):
    placeholders = ",".join(["(%s, %s)"] * len(ids))
    params = [carrier]
    for o_id in ids:
        params.extend([o_id, w_id])
    cursor.execute(
        f"UPDATE ORDERS SET O_CARRIER_ID = %s WHERE (O_ID, O_W_ID) IN ({placeholders})",
        params,
    )
    return []
'''

STATIC_BATCHED = '''
def doDelivery(cursor, ids, w_id, carrier):
    cursor.execute("SELECT O_ID, O_CARRIER_ID FROM ORDERS WHERE O_ID = ANY(%s)", (ids,))
    return {row[0]: row[1] for row in cursor.fetchall()}
'''


def test_dynamic_sql_rewrite_passes_but_reports_gaps():
    gaps: list[dict] = []
    result = verify_contract(UNBATCHED, DYNAMIC_BATCHED, _contract(), gaps=gaps)

    assert result.status == "PASS"
    assert result.verification_status == "PARTIAL"
    assert result.gaps
    assert gaps  # collector was extended
    assert {g.check for g in result.gaps} & {
        "check_placeholder_param_mismatch",
        "check_dynamic_placeholder_arity",
    }


def test_static_rewrite_is_fully_verified():
    result = verify_contract(UNBATCHED, STATIC_BATCHED, _contract())

    assert result.status == "PASS"
    assert result.gaps == []
    assert result.verification_status == "FULL"


def _finalize_state(policy: str) -> dict:
    return {
        "gap_policy": policy,
        "target_results": [
            {
                "file": "a.py",
                "function": "f",
                "status": "PASS",
                "verification": {
                    "status": "PASS",
                    "gaps": [
                        {
                            "check": "check_placeholder_param_mismatch",
                            "function": "f",
                            "line": 10,
                            "reason": "dynamic SQL or unknown parameter count",
                        }
                    ],
                },
            }
        ],
        "verifier_output": {"status": "PASS", "category": "NONE"},
    }


def test_finalize_warn_keeps_pass_and_surfaces_gap():
    event = finalize(_Ctx(_finalize_state("warn")))
    delta = event.actions.state_delta
    verdict = delta["verifier_output"]

    assert verdict["status"] == "PASS"
    assert "[GAP] check_placeholder_param_mismatch:" in verdict["detail"]
    assert delta["deterministic_verification"]["verification_status"] == "PARTIAL"


def test_finalize_fail_policy_turns_partial_into_fail():
    event = finalize(_Ctx(_finalize_state("fail")))
    delta = event.actions.state_delta
    verdict = delta["verifier_output"]

    assert verdict["status"] == "FAIL"
    assert verdict["category"] == "unverified"
    assert delta["deterministic_verification"]["status"] == "FAIL"
    assert "check_placeholder_param_mismatch" in verdict["reason"]


def test_write_output_result_reliability_block(tmp_path):
    out = tmp_path / "result.json"
    state = {
        "target": "/tmp/target",
        "sandbox": None,
        "modified_files": [],
        "gap_policy": "fail",
        "smoke_evidence": {"entry": "main", "startup": "ok"},
        "deterministic_verification": {
            "status": "FAIL",
            "verification_status": "PARTIAL",
            "checks": [{"name": "check_syntax", "status": "PASS", "details": ""}],
            "gaps": [
                {
                    "check": "check_row_indices",
                    "function": "f",
                    "line": 3,
                    "reason": "SELECT column count not resolvable",
                }
            ],
        },
        "verifier_output": {"status": "FAIL"},
    }

    _write_output_result(str(out), state, model="test-model")
    data = json.loads(out.read_text())

    reliability = data["reliability"]
    assert reliability["verification_status"] == "PARTIAL"
    assert reliability["gap_policy"] == "fail"
    assert reliability["gap_count"] == 1
    assert reliability["gaps"][0]["check"] == "check_row_indices"
    assert reliability["checks_run"] == ["check_syntax"]
    assert reliability["evidence"] == {
        "static": "FAIL",
        "smoke": {"entry": "main", "startup": "ok"},
    }
    assert data["verdict_basis"]["gap_policy"] == "fail"


def test_write_output_result_checks_run_from_target_results(tmp_path):
    out = tmp_path / "result.json"
    state = {
        "target_results": [
            {
                "verification": {
                    "checks": [
                        {"name": "check_syntax", "status": "PASS", "details": ""},
                        {"name": "check_row_indices", "status": "PASS", "details": ""},
                    ]
                }
            },
            {
                "verification": {
                    "checks": [
                        {"name": "check_syntax", "status": "PASS", "details": ""},
                        {
                            "name": "check_placeholder_param_mismatch",
                            "status": "PASS",
                            "details": "",
                        },
                    ]
                }
            },
        ],
        # Must be ignored when target results carry checks.
        "deterministic_verification": {
            "status": "PASS",
            "checks": [{"name": "check_fallback_only"}],
        },
    }

    _write_output_result(str(out), state)
    data = json.loads(out.read_text())

    assert data["reliability"]["checks_run"] == [
        "check_placeholder_param_mismatch",
        "check_row_indices",
        "check_syntax",
    ]


def test_write_output_result_checks_run_falls_back_without_target_results(tmp_path):
    out = tmp_path / "result.json"
    state = {
        "target_results": [],
        "deterministic_verification": {
            "status": "PASS",
            "checks": [
                {"name": "check_syntax", "status": "PASS", "details": ""},
                {"name": "check_dependency_integrity", "status": "PASS", "details": ""},
            ],
        },
    }

    _write_output_result(str(out), state)
    data = json.loads(out.read_text())

    assert data["reliability"]["checks_run"] == [
        "check_dependency_integrity",
        "check_syntax",
    ]


def _many_gap_source(functions: int, sites: int) -> str:
    lines = []
    for fn in range(functions):
        lines.append(f"def f{fn}(cursor, ids):")
        lines.append('    ph = ",".join(["%s"] * len(ids))')
        for _ in range(sites):
            lines.append(
                '    cursor.execute(f"SELECT A FROM T WHERE ID IN ({ph})", ids)'
            )
        lines.append("    return []")
    return "\n".join(lines)


def test_gap_dedupe_and_caps(tmp_path):
    source = _many_gap_source(3, 12)
    target = tmp_path / "target"
    sandbox = tmp_path / "sandbox"
    target.mkdir()
    sandbox.mkdir()
    (target / "repo.py").write_text(source)
    (sandbox / "repo.py").write_text(source)

    # One synthetic function with many unresolvable sites: per-contract cap.
    single = verify_contract(source, source, _contract("f0", "repo.py"))
    assert len(single.gaps) <= 20

    contracts = [
        RewriteContract(
            rewrite_id=f"gap-{fn}",
            target=RewriteTarget(file="repo.py", function=f"f{fn}"),
            targets=[RewriteTarget(file="repo.py", function=f"f{fn}")],
            pattern="N+1 Query",
            strategy="Query Batching",
            allowed_regions=[f"f{fn}"],
        )
        for fn in range(3)
    ]
    result = verify_all_contracts(
        str(target), str(sandbox), contracts, ["repo.py"]
    )
    assert 0 < len(result.gaps) <= 50
    keys = [
        (g.check, g.function, g.line, g.reason) for g in result.gaps
    ]
    assert len(keys) == len(set(keys))
    assert result.verification_status == "PARTIAL"
