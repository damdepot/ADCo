import pytest
from pathlib import Path
import os

from src.code_rewriter.models.rewrite_models import RewriteContract, RewriteTarget
from src.code_rewriter.tools.rewrite_verifier import verify_rewrite
from src.code_rewriter.models.verification_models import VerificationResult

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "code_rewriter"

def read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()

@pytest.fixture
def contract():
    return RewriteContract(
        rewrite_id="test_123",
        target=RewriteTarget(file="test.py", function="get_user_data"),
        pattern="N+1 Query",
        strategy="Replace loop with IN clause",
        allowed_regions=["get_user_data"],
        must_preserve=["return_type", "function_signature"],
        must_not_change=[]
    )

def test_valid_rewrite(contract):
    # Test 1: Valid rewrite (PASS)
    # Test 7: Valid SQL transformation allowed
    # Test 8: Unrelated function unchanged
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("optimized_n_plus_one.py")
    
    result = verify_rewrite(orig, opt, contract)
    assert result.status == "PASS"
    assert len(result.violations) == 0

def test_syntax_error(contract):
    # Test 2: Syntax error
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("invalid_optimized.py")
    
    result = verify_rewrite(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "SYNTAX_ERROR" for v in result.violations)

def test_signature_changed(contract):
    # Test 3: Signature changed
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("signature_changed.py")
    
    result = verify_rewrite(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "FUNCTION_SIGNATURE_CHANGED" for v in result.violations)

def test_target_missing(contract):
    # Test 4: Target removed / missing
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("target_missing.py")
    
    result = verify_rewrite(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "TARGET_MISSING" for v in result.violations)

def test_unauthorized_change(contract):
    # Test 5: Unauthorized function changed
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("unauthorized_change.py")
    
    result = verify_rewrite(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "UNAUTHORIZED_CHANGE" for v in result.violations)

def test_strategy_not_applied(contract):
    # Test 6: Strategy not applied
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("strategy_not_applied.py")
    
    result = verify_rewrite(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "STRATEGY_NOT_APPLIED" for v in result.violations)

def test_return_behavior_changed(contract):
    # Test 9: Return behavior changed
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("return_behavior_changed.py")
    
    result = verify_rewrite(orig, opt, contract)
    assert result.status == "FAIL"
    assert any(v.code == "RETURN_BEHAVIOR_CHANGED" for v in result.violations)

def test_determinism(contract):
    # Test 10: Determinism
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("optimized_n_plus_one.py")
    
    res1 = verify_rewrite(orig, opt, contract)
    res2 = verify_rewrite(orig, opt, contract)
    
    assert res1.model_dump() == res2.model_dump()

def test_checks_visibility_and_serialization(contract):
    # Test 11: Checks visibility in result
    # Test 12: model_dump() serialization
    orig = read_fixture("original_n_plus_one.py")
    opt = read_fixture("optimized_n_plus_one.py")
    
    result = verify_rewrite(orig, opt, contract)
    assert len(result.checks) > 0
    assert any(c.name == "check_syntax" for c in result.checks)
    
    dump = result.model_dump()
    assert dump["status"] == "PASS"
    assert "checks" in dump
    assert "violations" in dump
