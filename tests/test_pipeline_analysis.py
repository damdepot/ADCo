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

def test_build_contracts_multi_file(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    
    file1 = target_dir / "repo1.py"
    file1.write_text("""
def get_users(ids):
    for user_id in ids:
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
""")
    file2 = target_dir / "repo2.py"
    file2.write_text("""
def get_orders(ids):
    for order_id in ids:
        cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
""")
    
    intent_output = {
        "optimization_targets": [
            {"file": "repo1.py"},
            {"file": "repo2.py"}
        ]
    }
    
    analyses, contracts = build_contracts_from_intent(str(target_dir), intent_output)
    assert len(contracts) == 2
    files = {c.target.file for c in contracts}
    assert files == {"repo1.py", "repo2.py"}
    
    c1 = next(c for c in contracts if c.target.file == "repo1.py")
    assert len(c1.targets) == 1
    assert c1.targets[0].function == "get_users"
    assert c1.allowed_regions == ["get_users"]

    c2 = next(c for c in contracts if c.target.file == "repo2.py")
    assert len(c2.targets) == 1
    assert c2.targets[0].function == "get_orders"
    assert c2.allowed_regions == ["get_orders"]

def test_build_contracts_filter_ddl_setup(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    
    file_path = target_dir / "schema_manager.py"
    file_path.write_text("""
def _execute_ddl(statements):
    for stmt in statements:
        cursor.execute(stmt)

def init_schema(tables):
    for t in tables:
        cursor.execute(f"CREATE TABLE {t}")

def get_products(ids):
    for pid in ids:
        cursor.execute("SELECT * FROM products WHERE id = ?", (pid,))
""")
    
    intent_output = {
        "optimization_targets": [
            {"file": "schema_manager.py"}
        ]
    }
    
    analyses, contracts = build_contracts_from_intent(str(target_dir), intent_output)
    assert len(contracts) == 1
    contract = contracts[0]
    # Only get_products should be a target; _execute_ddl and init_schema should be excluded
    target_func_names = [t.function for t in contract.targets]
    assert target_func_names == ["get_products"]
    assert contract.allowed_regions == ["get_products"]

def test_execute_deterministic_verification_multi_file_scoped(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    
    orig_f1 = target_dir / "repo1.py"
    orig_f1.write_text("""
def get_users(ids):
    for user_id in ids:
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
""")
    opt_f1 = sandbox_dir / "repo1.py"
    opt_f1.write_text("""
def get_users(ids):
    cursor.execute("SELECT * FROM users WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
""")
    
    orig_f2 = target_dir / "repo2.py"
    orig_f2.write_text("""
def get_orders(ids):
    for order_id in ids:
        cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
""")
    # repo2 is NOT modified in sandbox
    
    intent_output = {
        "optimization_targets": [
            {"file": "repo1.py"},
            {"file": "repo2.py"}
        ]
    }
    
    analyses, contracts = build_contracts_from_intent(str(target_dir), intent_output)
    assert len(contracts) == 2
    
    # Only repo1.py was modified by the agent
    result = execute_deterministic_verification(
        str(target_dir), 
        str(sandbox_dir), 
        contracts, 
        modified_files=["repo1.py"]
    )
    assert result.status == "PASS"
    assert result.expected_targets == 1
    assert result.transformed_targets == 1
    assert result.missing_targets == 0
    assert result.rewrite_coverage == 1.0

