"""Re-verifiability guarantees: round-trip, replay, sandbox digest, honest coverage."""

import json
import re
import shutil
from pathlib import Path

import pytest

from src.code_rewriter.main import _sandbox_digest, _write_output_result
from src.code_rewriter.models import RewriteContract, RewriteTarget
from src.code_rewriter.tools.contract_verifier import verify_contract
from src.code_rewriter.tools.pipeline_analysis import verify_all_contracts

FIXTURES = Path("tests/fixtures/code_rewriter")


def _contract(
    function: str, rewrite_required: bool = True, allowed: list[str] | None = None
) -> RewriteContract:
    """A one-target N+1 contract; JSON-safe so it survives a model_dump round trip."""
    return RewriteContract(
        rewrite_id=f"c_{function}",
        target=RewriteTarget(file="app.py", function=function, qualified_function=function),
        targets=[RewriteTarget(file="app.py", function=function, qualified_function=function)],
        pattern="N_PLUS_ONE_QUERY",
        strategy="BATCH",
        allowed_regions=allowed or [function],
        rewrite_required=rewrite_required,
    )


def test_contract_round_trip_verifies_identically():
    """A persisted contract re-verifies to the same verdict as the in-memory one."""
    original = (FIXTURES / "original_n_plus_one.py").read_text()
    optimized = (FIXTURES / "optimized_n_plus_one.py").read_text()
    contract = _contract("get_user_data")
    rehydrated = RewriteContract.model_validate(contract.model_dump())

    first = verify_contract(original, optimized, contract)
    again = verify_contract(original, optimized, rehydrated)

    assert rehydrated == contract
    assert rehydrated.rewrite_required is True
    assert again.status == first.status
    assert [v.code for v in again.violations] == [v.code for v in first.violations]
    assert again.rewrite_coverage == first.rewrite_coverage


def test_replay_reproduces_persisted_verdict(tmp_path):
    """A recorded PASS is reproducible from the persisted contracts + sandbox."""
    target_dir, sandbox = tmp_path / "target", tmp_path / "sandbox"
    target_dir.mkdir()
    sandbox.mkdir()
    shutil.copy(FIXTURES / "original_n_plus_one.py", target_dir / "app.py")
    shutil.copy(FIXTURES / "optimized_n_plus_one.py", sandbox / "app.py")

    modified = ["app.py"]
    recorded = verify_all_contracts(
        str(target_dir), str(sandbox), [_contract("get_user_data")], modified
    )
    assert recorded.status == "PASS"

    out = tmp_path / "out" / "result.json"
    _write_output_result(
        str(out),
        {
            "target": str(target_dir),
            "sandbox": str(sandbox),
            "modified_files": modified,
            "rewrite_contracts": [_contract("get_user_data").model_dump()],
            "verifier_output": {"status": recorded.status},
            "deterministic_verification": recorded.model_dump(),
        },
        model="test-model",
    )
    data = json.loads(out.read_text())
    replayed = verify_all_contracts(
        data["target"],
        data["verdict_basis"]["sandbox"],
        [RewriteContract.model_validate(c) for c in data["rewrite_contracts"]],
        data["modified_files"],
    )
    persisted = data["outputs"]["deterministic_verification"]
    assert persisted["status"] == "PASS"
    assert data["verdict_basis"]["coverage_basis"] == "required_targets"
    assert replayed.expected_targets == persisted["expected_targets"] == 1
    assert replayed.status == persisted["status"]
    assert replayed.rewrite_coverage == pytest.approx(persisted["rewrite_coverage"])


def test_sandbox_digest_is_meaningful(tmp_path):
    """Digests are sandbox-relative sha256 values that track file content."""
    sandbox = tmp_path / "sandbox"
    (sandbox / "pkg").mkdir(parents=True)
    (sandbox / "pkg" / "app.py").write_text("x = 1\n")
    state = {"sandbox": str(sandbox), "modified_files": ["pkg/app.py"]}
    out = tmp_path / "result.json"

    _write_output_result(str(out), dict(state), model="test-model")
    digest = json.loads(out.read_text())["sandbox_digest"]
    assert set(digest) == {"pkg/app.py"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in digest.values())

    (sandbox / "pkg" / "app.py").write_text("x = 2\n")
    _write_output_result(str(out), dict(state), model="test-model")
    assert json.loads(out.read_text())["sandbox_digest"]["pkg/app.py"] != digest["pkg/app.py"]

    _write_output_result(str(out), {}, model="test-model")
    assert json.loads(out.read_text())["sandbox_digest"] == {}
    assert _sandbox_digest(None, ["pkg/app.py"]) == {}


def test_noop_target_cannot_rescue_or_deflate_coverage(tmp_path):
    """Coverage is measured over required targets only: 1 of 2 rewritten is 0.5."""
    target_dir, sandbox = tmp_path / "target", tmp_path / "sandbox"
    target_dir.mkdir()
    sandbox.mkdir()
    shutil.copy(FIXTURES / "partial_original.py", target_dir / "app.py")
    shutil.copy(FIXTURES / "partial_two_transformed.py", sandbox / "app.py")
    modified = ["app.py"]
    # get_product is rewritten, get_supplier is not, authenticate is loop-free.
    targets = ["Repository.get_product", "Repository.get_supplier", "Repository.authenticate"]
    rewritten = [_contract(fn, allowed=targets) for fn in targets[:2]]

    baseline = verify_all_contracts(str(target_dir), str(sandbox), rewritten, modified)
    assert baseline.status == "FAIL"
    assert baseline.rewrite_coverage == 0.5

    with_noop = verify_all_contracts(
        str(target_dir),
        str(sandbox),
        rewritten + [_contract("Repository.authenticate", False, allowed=targets)],
        modified,
    )
    assert with_noop.status == "FAIL"
    # A loop-free target neither rescues (2/3) nor deflates (1/3) the ratio.
    assert with_noop.rewrite_coverage == 0.5
    assert with_noop.expected_targets == 2
    assert with_noop.transformed_targets == 1
    assert any(v.code == "MISSING_REWRITE" for v in with_noop.violations)
    noop = next(tc for tc in with_noop.target_coverage if "authenticate" in tc.function)
    assert noop.status == "NO_OP"


def test_rewritten_loop_free_target_cannot_push_coverage_above_one(tmp_path):
    """A loop-free target rewritten anyway stays out of the ratio (max 1.0)."""
    orig = """
class Repo:
    def get_items(self, ids):
        out = []
        for i in ids:
            self.cursor.execute("SELECT a FROM t WHERE id = %s", (i,))
            out.append(self.cursor.fetchone())
        return out

    def ping(self, x):
        self.cursor.execute("SELECT 1 FROM t WHERE id = %s", (x,))
        return self.cursor.fetchone()
"""
    opt = """
class Repo:
    def get_items(self, ids):
        self.cursor.execute("SELECT a FROM t WHERE id = ANY(%s)", (ids,))
        return self.cursor.fetchall()

    def ping(self, x):
        self.cursor.execute("SELECT id FROM t WHERE id = ANY(%s)", ([x],))
        return self.cursor.fetchone()
"""
    targets = ["Repo.get_items", "Repo.ping"]

    def contract(fn, required):
        return RewriteContract(
            rewrite_id=f"c_{fn}",
            target=RewriteTarget(file="app.py", function=fn, qualified_function=fn),
            targets=[RewriteTarget(file="app.py", function=fn, qualified_function=fn)],
            pattern="N_PLUS_ONE_QUERY",
            strategy="BATCH",
            allowed_regions=targets,
            rewrite_required=required,
        )

    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "app.py").write_text(orig)
    (tmp_path / "sandbox").mkdir()
    (tmp_path / "sandbox" / "app.py").write_text(opt)
    # ping is loop-free (rewrite_required=False) yet the optimizer rewrote it.
    result = verify_all_contracts(
        str(tmp_path / "target"),
        str(tmp_path / "sandbox"),
        [contract("Repo.get_items", True), contract("Repo.ping", False)],
        ["app.py"],
    )

    assert result.rewrite_coverage == 1.0
    assert result.transformed_targets == 2  # informational: all transformed targets
    assert result.expected_targets == 1
    assert result.missing_targets == 0
    assert "(1/1)" in result.summary
    assert "2/1" not in result.summary
    for check in result.checks:
        assert "2/1" not in check.details
