import os
import pytest
from src.code_rewriter.tools.pipeline_analysis import (
    build_contracts_from_intent,
    execute_deterministic_verification,
    format_pipeline_analysis_markdown,
    format_dependency_slice_map,
    verify_target,
)
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
    
    # Only repo1.py was modified by the agent; repo2 target is unverified → FAIL
    result = execute_deterministic_verification(
        str(target_dir), 
        str(sandbox_dir), 
        contracts, 
        modified_files=["repo1.py"]
    )
    assert result.status == "FAIL"
    assert result.expected_targets == 2
    assert result.transformed_targets == 1
    assert result.missing_targets == 1
    assert result.rewrite_coverage == 0.5


def test_format_pipeline_analysis_markdown(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    file_path = target_dir / "repo.py"
    file_path.write_text("""def get_users(ids):
    for user_id in ids:
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
""")

    intent_output = {
        "optimization_targets": [
            {"file": "repo.py"}
        ]
    }
    analyses, contracts = build_contracts_from_intent(str(target_dir), intent_output)
    md = format_pipeline_analysis_markdown(analyses, contracts, target_dir=str(target_dir))
    assert "Dependency Slice: `get_users`" in md
    assert "Database Operations" in md
    assert "LOOP / N+1 RISK" in md

    slice_map = format_dependency_slice_map(analyses, contracts, str(target_dir))
    assert "get_users" in slice_map
    assert slice_map["get_users"] == md


def test_build_contracts_per_target_pattern(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    file_path = target_dir / "repo.py"
    file_path.write_text('''
def get_users(ids):
    for user_id in ids:
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))

def sync_users(ids, names):
    cursor.execute("SELECT * FROM users WHERE id = ?", (ids,))
    cursor.execute("UPDATE users SET name = ? WHERE id = ?", (names, ids))
''')

    intent_output = {
        "optimization_targets": [
            {"file": "repo.py"}
        ]
    }

    analyses, contracts = build_contracts_from_intent(str(target_dir), intent_output)
    assert len(contracts) == 2
    assert all(len(c.targets) == 1 for c in contracts)

    loop_contract = next(c for c in contracts if c.target.function == "get_users")
    seq_contract = next(c for c in contracts if c.target.function == "sync_users")

    assert loop_contract.pattern != "SEQUENTIAL_CHAIN"
    assert "N" in loop_contract.pattern or loop_contract.pattern == "N_PLUS_ONE_QUERY"
    assert loop_contract.targets[0].function == "get_users"
    assert loop_contract.allowed_regions == ["get_users"]

    assert seq_contract.pattern == "SEQUENTIAL_CHAIN"
    assert seq_contract.targets[0].function == "sync_users"
    assert seq_contract.allowed_regions == ["sync_users"]


def test_verify_target_pass_and_missing(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    (target_dir / "repo.py").write_text('''
def get_users(ids):
    for user_id in ids:
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
''')
    (sandbox_dir / "repo.py").write_text('''
def get_users(ids):
    cursor.execute("SELECT * FROM users WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
''')

    intent_output = {
        "optimization_targets": [
            {"file": "repo.py"}
        ]
    }
    _analyses, contracts = build_contracts_from_intent(str(target_dir), intent_output)
    assert len(contracts) == 1

    result = verify_target(str(target_dir), str(sandbox_dir), contracts[0])
    assert result.status == "PASS"

    missing_sandbox = tmp_path / "missing_sandbox"
    missing_sandbox.mkdir()
    missing_result = verify_target(str(target_dir), str(missing_sandbox), contracts[0])
    assert missing_result.status == "FAIL"
    assert any(v.code == "MISSING_REWRITE" for v in missing_result.violations)
    assert missing_result.missing_targets == 1
    assert missing_result.rewrite_coverage == 0.0


def test_execute_deterministic_verification_warning_only_passes(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()

    (target_dir / "repo.py").write_text('''
def get_users(ids):
    result = []
    for uid in ids:
        cursor.execute("SELECT * FROM users WHERE id = ?", (uid,))
        result.append(cursor.fetchone())
    assert result
    return result
''')
    (sandbox_dir / "repo.py").write_text('''
def get_users(ids):
    if not ids:
        return []
    cursor.execute("SELECT * FROM users WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
    return cursor.fetchall()
''')

    contracts = [
        RewriteContract(
            rewrite_id="test-warning",
            target=RewriteTarget(file="repo.py", function="get_users"),
            pattern="N_PLUS_ONE_QUERY",
            strategy="COMBINING_QUERIES",
            targets=[RewriteTarget(file="repo.py", function="get_users")]
        )
    ]

    result = execute_deterministic_verification(
        str(target_dir),
        str(sandbox_dir),
        contracts,
        ["repo.py"]
    )
    assert result.status == "PASS"
    assert result.transformed_targets == result.expected_targets
    assert any(v.code == "ASSERT_REMOVED" and v.severity == "WARNING" for v in result.violations)


