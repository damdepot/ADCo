from typing import Optional

from ..models.ast_models import FileAnalysis
from ..models.rewrite_models import RewriteContract, RewriteTarget

def build_rewrite_contract(
    analysis: FileAnalysis,
    target: RewriteTarget,
    pattern: str,
    strategy: str,
    required_conditions: Optional[list[str]] = None,
    must_preserve: Optional[list[str]] = None,
    must_not_change: Optional[list[str]] = None,
    rewrite_id: Optional[str] = None,
) -> RewriteContract:
    """
    Build a RewriteContract based on the provided analysis and target.
    
    Validates that the target function/method exists in the given FileAnalysis.
    If only a file is targeted, validation passes.
    """
    # Validate target and find source location
    source_location = None
    if target.qualified_function or target.function:
        found = False
        target_name = target.qualified_function or target.function
        
        # Search top-level functions
        for func in analysis.functions:
            if func.qualified_name == target_name or func.name == target_name:
                found = True
                source_location = func.source_location
                break
                
        # Search class methods if not found
        if not found:
            for cls in analysis.classes:
                for method in cls.methods:
                    if method.qualified_name == target_name or method.name == target_name:
                        found = True
                        source_location = method.source_location
                        break
                        
        if not found:
            raise ValueError(f"Target function '{target_name}' not found in analysis.")
            
        if not target.source_location:
            target.source_location = source_location

    import uuid
    _id = rewrite_id if rewrite_id is not None else str(uuid.uuid4())
    
    if must_preserve is None:
        must_preserve = ["Existing business logic", "Return types"]
    if must_not_change is None:
        must_not_change = ["Database schema", "External API contracts"]

    return RewriteContract(
        rewrite_id=_id,
        target=target,
        pattern=pattern,
        strategy=strategy,
        allowed_regions=[],
        required_conditions=required_conditions or [],
        must_preserve=must_preserve,
        must_not_change=must_not_change,
    )
