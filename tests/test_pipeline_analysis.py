import os
import pytest
from src.code_rewriter.tools.pipeline_analysis import build_contracts_from_intent, execute_deterministic_verification, serialize_verification_results
from src.code_rewriter.models.rewrite_models import RewriteContract, RewriteTarget

def test_build_contracts_from_intent(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    
    file_path = target_dir / "repo.py"
    file_path.write_text("""
def get_users(ids):
    for user_id in ids:
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    """)
    
    intent_output = {
        "optimization_targets": [
            {"file": "repo.py"}
        ]
    }
    
    analyses, contracts = build_contracts_from_intent(str(target_dir), intent_output)
    assert "repo.py" in analyses
    assert len(contracts) == 1
    assert contracts[0].target.file == "repo.py"
    assert contracts[0].target.function == "get_users"

def test_execute_deterministic_verification_missing_rewrite(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    
    contracts = [
        RewriteContract(
            rewrite_id="test-123",
            target=RewriteTarget(file="repo.py", function="get_users"),
            pattern="N_PLUS_ONE_QUERY",
            strategy="COMBINING_QUERIES",
            targets=[RewriteTarget(file="repo.py", function="get_users")]
        )
    ]
    
    # Empty modified files
    result = execute_deterministic_verification(str(target_dir), str(sandbox_dir), contracts, [])
    assert result.status == "FAIL"
    assert result.missing_targets == 1
    assert result.rewrite_coverage == 0.0

def test_serialize_verification_results():
    from src.code_rewriter.tools.rewrite_verifier import VerificationResult
    res = VerificationResult(
        status="PASS",
        violations=[],
        checks=[],
        summary="Test",
        target_coverage=[],
        expected_targets=0,
        transformed_targets=0,
        missing_targets=0,
        rewrite_coverage=1.0
    )
    serialized = serialize_verification_results(res)
    assert serialized["status"] == "PASS"
