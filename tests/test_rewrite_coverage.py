import pytest
from src.code_rewriter.models import RewriteContract, RewriteTarget
from src.code_rewriter.tools.rewrite_verifier import verify_rewrite
from src.code_rewriter.tools.ast_analyzer import analyze_source

def read_fixture(name):
    with open(f"tests/fixtures/code_rewriter/{name}.py", "r") as f:
        return f.read()

@pytest.fixture
def original_source():
    return read_fixture("partial_original")

@pytest.fixture
def contract():
    return RewriteContract(
        rewrite_id="test_partial",
        target=RewriteTarget(file="partial.py"),
        targets=[
            RewriteTarget(file="partial.py", function="Repository.get_product", qualified_function="Repository.get_product"),
            RewriteTarget(file="partial.py", function="Repository.get_category", qualified_function="Repository.get_category"),
            RewriteTarget(file="partial.py", function="Repository.get_supplier", qualified_function="Repository.get_supplier"),
        ],
        pattern="N+1 query elimination",
        strategy="BATCH query",
        allowed_regions=["Repository.get_product", "Repository.get_category", "Repository.get_supplier"],
        must_preserve=["return_type"]
    )

def test_all_targets_transformed(original_source, contract):
    opt_source = read_fixture("partial_all_transformed")
    result = verify_rewrite(original_source, opt_source, contract)
    
    assert result.status == "PASS"
    assert result.expected_targets == 3
    assert result.transformed_targets == 3
    assert result.missing_targets == 0
    assert result.rewrite_coverage == 1.0
    
    coverage_check = next(c for c in result.checks if c.name == "check_rewrite_coverage")
    assert coverage_check.status == "PASS"

def test_partial_rewrite_two_of_three(original_source, contract):
    opt_source = read_fixture("partial_two_transformed")
    result = verify_rewrite(original_source, opt_source, contract)
    
    assert result.status == "FAIL"
    assert result.expected_targets == 3
    assert result.transformed_targets == 2
    assert result.missing_targets == 1
    assert round(result.rewrite_coverage, 3) == 0.667
    
    coverage_check = next(c for c in result.checks if c.name == "check_rewrite_coverage")
    assert coverage_check.status == "FAIL"
    
    missing_violation = next(v for v in result.violations if v.code == "MISSING_REWRITE")
    assert "Repository.get_supplier" in missing_violation.message

def test_no_targets_transformed(original_source, contract):
    opt_source = read_fixture("partial_none_transformed")
    result = verify_rewrite(original_source, opt_source, contract)
    
    assert result.status == "FAIL"
    assert result.expected_targets == 3
    assert result.transformed_targets == 0
    assert result.missing_targets == 3
    assert result.rewrite_coverage == 0.0

def test_unauthorized_unrelated_change(original_source, contract):
    opt_source = read_fixture("partial_unauthorized")
    result = verify_rewrite(original_source, opt_source, contract)
    
    assert result.status == "FAIL"
    unauth_violation = next(v for v in result.violations if v.code == "UNAUTHORIZED_CHANGE")
    assert "authenticate" in unauth_violation.message

def test_coverage_metrics_fields_present(original_source):
    opt_source = read_fixture("partial_all_transformed")
    empty_contract = RewriteContract(
        rewrite_id="empty",
        target=RewriteTarget(file="partial.py", function="Repository.get_product", qualified_function="Repository.get_product"),
        targets=[],
        pattern="N+1 query elimination",
        strategy="BATCH",
        allowed_regions=["Repository.get_product"]
    )
    result = verify_rewrite(original_source, opt_source, empty_contract)
    
    assert result.status == "PASS"
    assert result.expected_targets == 0
    assert result.transformed_targets == 0
    assert result.missing_targets == 0
    assert result.rewrite_coverage == 1.0
    
    coverage_check = next(c for c in result.checks if c.name == "check_rewrite_coverage")
    assert coverage_check.status == "PASS"

def test_target_coverage_per_function_details(original_source, contract):
    opt_source = read_fixture("partial_two_transformed")
    result = verify_rewrite(original_source, opt_source, contract)
    
    assert len(result.target_coverage) == 3
    
    prod_cov = next(tc for tc in result.target_coverage if "get_product" in tc.function)
    assert prod_cov.status == "TRANSFORMED"
    
    cat_cov = next(tc for tc in result.target_coverage if "get_category" in tc.function)
    assert cat_cov.status == "TRANSFORMED"
    
    sup_cov = next(tc for tc in result.target_coverage if "get_supplier" in tc.function)
    assert sup_cov.status == "MISSING_REWRITE"

def test_coverage_determinism(original_source, contract):
    opt_source = read_fixture("partial_two_transformed")
    result1 = verify_rewrite(original_source, opt_source, contract)
    result2 = verify_rewrite(original_source, opt_source, contract)
    
    assert result1.model_dump() == result2.model_dump()

