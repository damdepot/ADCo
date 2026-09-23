import pytest

from src.code_rewriter.models.ast_models import (
    FileAnalysis, 
    FunctionAnalysis, 
    SourceLocation,
    ClassAnalysis
)
from src.code_rewriter.models.rewrite_models import RewriteTarget
from src.code_rewriter.tools.rewrite_contract import build_rewrite_contract

def test_build_rewrite_contract_valid_function_target():
    analysis = FileAnalysis(
        file_path="test.py",
        functions=[
            FunctionAnalysis(
                name="get_users",
                qualified_name="get_users",
                source_location=SourceLocation(start_line=10, end_line=20, start_column=0, end_column=0)
            )
        ]
    )
    
    target = RewriteTarget(file="test.py", function="get_users")
    contract = build_rewrite_contract(
        analysis=analysis,
        target=target,
        pattern="N+1 query",
        strategy="Batching",
        must_preserve=["semantics"]
    )
    
    assert contract.target.file == "test.py"
    assert contract.target.function == "get_users"
    assert contract.target.source_location is not None
    assert contract.target.source_location.start_line == 10
    assert contract.pattern == "N+1 query"
    assert contract.strategy == "Batching"
    assert len(contract.must_preserve) == 1
    assert contract.must_preserve[0] == "semantics"
    assert contract.rewrite_id is not None

def test_build_rewrite_contract_valid_method_target():
    analysis = FileAnalysis(
        file_path="test.py",
        classes=[
            ClassAnalysis(
                name="UserRepository",
                methods=[
                    FunctionAnalysis(
                        name="get_users",
                        qualified_name="UserRepository.get_users",
                        source_location=SourceLocation(start_line=15, end_line=25, start_column=4, end_column=4)
                    )
                ],
                source_location=SourceLocation(start_line=10, end_line=30, start_column=0, end_column=0)
            )
        ]
    )
    
    target = RewriteTarget(file="test.py", qualified_function="UserRepository.get_users")
    contract = build_rewrite_contract(
        analysis=analysis,
        target=target,
        pattern="N+1 query",
        strategy="Batching",
    )
    
    assert contract.target.source_location is not None
    assert contract.target.source_location.start_line == 15

def test_build_rewrite_contract_invalid_target_raises_value_error():
    analysis = FileAnalysis(file_path="test.py")
    target = RewriteTarget(file="test.py", function="non_existent")
    
    with pytest.raises(ValueError, match="Target function 'non_existent' not found in analysis."):
        build_rewrite_contract(
            analysis=analysis,
            target=target,
            pattern="N+1 query",
            strategy="Batching",
        )

def test_build_rewrite_contract_file_only_target():
    analysis = FileAnalysis(file_path="test.py")
    target = RewriteTarget(file="test.py")
    
    contract = build_rewrite_contract(
        analysis=analysis,
        target=target,
        pattern="Refactoring",
        strategy="Modularization",
    )
    
    assert contract.target.file == "test.py"
    assert contract.target.function is None

def test_build_rewrite_contract_defaults():
    analysis = FileAnalysis(file_path="test.py")
    target = RewriteTarget(file="test.py")
    
    contract = build_rewrite_contract(
        analysis=analysis,
        target=target,
        pattern="Refactoring",
        strategy="Modularization",
    )
    
    assert "return_type" in contract.must_preserve
    assert "transaction_semantics" in contract.must_preserve
    assert "Database schema" in contract.must_not_change
