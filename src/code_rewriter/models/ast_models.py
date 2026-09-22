from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class SourceLocation(BaseModel):
    """Represents a location within a source file."""
    start_line: int = Field(description="Starting line number (1-indexed)")
    end_line: int = Field(description="Ending line number")
    start_column: int = Field(description="Starting column offset")
    end_column: int = Field(description="Ending column offset")


class ControlFlowAnalysis(BaseModel):
    """Analysis of control flow structures in a function."""
    for_loops: int = Field(default=0, description="Number of for loops")
    while_loops: int = Field(default=0, description="Number of while loops")


class CallAnalysis(BaseModel):
    """Analysis of a function call."""
    call_name: str = Field(description="The name of the function or method being called")
    receiver: Optional[str] = Field(default=None, description="The object receiving the method call, if any")
    containing_function: Optional[str] = Field(default=None, description="The fully qualified name of the function containing this call")
    source_location: SourceLocation = Field(description="The location of the call in the source code")


SqlOperation = Literal["SELECT", "INSERT", "UPDATE", "DELETE", "OTHER", "UNKNOWN"]
DbOperationType = Literal[
    "EXECUTE", "EXECUTEMANY", "FETCH", "COMMIT", "ROLLBACK", "CURSOR", "CONNECT", "UNKNOWN"
]


class DatabaseOperation(BaseModel):
    """Analysis of a database operation."""
    call_name: str = Field(description="The name of the database driver method being called")
    operation_type: DbOperationType = Field(description="The categorized type of the database operation")
    sql_operation: SqlOperation = Field(description="The type of SQL statement being executed")
    sql: Optional[str] = Field(default=None, description="The statically determinable SQL string, if available")
    containing_function: Optional[str] = Field(default=None, description="The fully qualified name of the function containing this operation")
    inside_loop: bool = Field(default=False, description="Whether this operation is performed inside a loop")
    loop_variables: List[str] = Field(default_factory=list, description="Variables bound by the enclosing loops")
    parameter_dependencies: List[str] = Field(default_factory=list, description="Parameters passed to the database operation that depend on local variables")
    source_location: SourceLocation = Field(description="The location of the database operation in the source code")


class ImportAnalysis(BaseModel):
    """Analysis of an import statement."""
    import_type: Literal["import", "from"] = Field(description="The type of import statement")
    module: Optional[str] = Field(default=None, description="The module being imported from, for from-imports")
    name: str = Field(description="The name being imported")
    alias: Optional[str] = Field(default=None, description="The alias the name is bound to, if any")
    source_location: SourceLocation = Field(description="The location of the import in the source code")


class FunctionAnalysis(BaseModel):
    """Analysis of a function or method."""
    name: str = Field(description="The name of the function")
    qualified_name: str = Field(description="The fully qualified name of the function")
    parameters: List[str] = Field(default_factory=list, description="The names of the function parameters")
    decorators: List[str] = Field(default_factory=list, description="The names of the decorators applied to the function")
    return_count: int = Field(default=0, description="The number of return statements in the function")
    control_flow: ControlFlowAnalysis = Field(default_factory=ControlFlowAnalysis, description="Analysis of the function's control flow")
    calls: List[CallAnalysis] = Field(default_factory=list, description="Analysis of calls made within the function")
    database_operations: List[DatabaseOperation] = Field(default_factory=list, description="Analysis of database operations performed within the function")
    source_location: SourceLocation = Field(description="The location of the function in the source code")


class ClassAnalysis(BaseModel):
    """Analysis of a class definition."""
    name: str = Field(description="The name of the class")
    base_classes: List[str] = Field(default_factory=list, description="The names of the base classes")
    methods: List[FunctionAnalysis] = Field(default_factory=list, description="Analysis of the methods defined in the class")
    source_location: SourceLocation = Field(description="The location of the class in the source code")


class FileAnalysis(BaseModel):
    """Comprehensive analysis of a source file."""
    file_path: str = Field(default="", description="The path to the analyzed file")
    parse_success: bool = Field(default=True, description="Whether the file was successfully parsed")
    parse_error: Optional[str] = Field(default=None, description="Error message if parsing failed")
    imports: List[ImportAnalysis] = Field(default_factory=list, description="Analysis of imports in the file")
    classes: List[ClassAnalysis] = Field(default_factory=list, description="Analysis of classes in the file")
    functions: List[FunctionAnalysis] = Field(default_factory=list, description="Analysis of top-level functions in the file")
    database_operations: List[DatabaseOperation] = Field(default_factory=list, description="Analysis of all database operations in the file")
