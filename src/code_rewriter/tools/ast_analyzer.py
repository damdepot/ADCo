import ast
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..models.ast_models import (
    CallAnalysis,
    ClassAnalysis,
    DatabaseOperation,
    DbOperationType,
    FileAnalysis,
    FunctionAnalysis,
    ImportAnalysis,
    SourceLocation,
    SqlOperation,
)
from .sql_resolver import collect_module_dicts, resolve

class _FileVisitor(ast.NodeVisitor):
    def __init__(self, module_dicts: Optional[Dict[str, Any]] = None):
        self.module_dicts: Dict[str, Any] = module_dicts if module_dicts is not None else {}
        self.imports: List[ImportAnalysis] = []
        self.classes: List[ClassAnalysis] = []
        self.functions: List[FunctionAnalysis] = []
        self.database_operations: List[DatabaseOperation] = []
        
        # Tracking state
        self.current_class: Optional[ClassAnalysis] = None
        self.current_function: Optional[FunctionAnalysis] = None
        self.function_stack: List[FunctionAnalysis] = []
        self.class_stack: List[ClassAnalysis] = []
        
        # State for loop variables and assignments
        self.loop_stack: List[List[str]] = []
        
        # Track local string assignments per function scope
        self.module_strings: Dict[str, str] = {}
        self.function_strings: List[Dict[str, str]] = []

        # Track resolved variable values per function scope
        self.module_vars: Dict[str, Any] = {}
        self.function_vars: List[Dict[str, Any]] = []

    def _get_location(self, node: ast.AST) -> SourceLocation:
        return SourceLocation(
            start_line=getattr(node, "lineno", -1),
            end_line=getattr(node, "end_lineno", getattr(node, "lineno", -1)),
            start_column=getattr(node, "col_offset", -1),
            end_column=getattr(node, "end_col_offset", getattr(node, "col_offset", -1)),
        )

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imports.append(ImportAnalysis(
                import_type="import",
                module=alias.name,
                name=alias.name,
                alias=alias.asname,
                source_location=self._get_location(node)
            ))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module_name = node.module or ""
        for alias in node.names:
            self.imports.append(ImportAnalysis(
                import_type="from",
                module=module_name,
                name=alias.name,
                alias=alias.asname,
                source_location=self._get_location(node)
            ))
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        base_classes = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                base_classes.append(base.id)
            elif isinstance(base, ast.Attribute):
                base_classes.append(base.attr)
                
        class_analysis = ClassAnalysis(
            name=node.name,
            base_classes=base_classes,
            source_location=self._get_location(node)
        )
        self.classes.append(class_analysis)
        self.class_stack.append(class_analysis)
        self.current_class = class_analysis
        
        self.generic_visit(node)
        
        self.class_stack.pop()
        self.current_class = self.class_stack[-1] if self.class_stack else None

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._handle_function_def(node)
        
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._handle_function_def(node)

    def _handle_function_def(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]):
        parameters = []
        for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
            parameters.append(arg.arg)
        if node.args.vararg:
            parameters.append(f"*{node.args.vararg.arg}")
        if node.args.kwarg:
            parameters.append(f"**{node.args.kwarg.arg}")
            
        decorators = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name):
                decorators.append(dec.id)
            elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
                decorators.append(dec.func.id)
                
        qual_name = node.name
        if self.current_class:
            qual_name = f"{self.current_class.name}.{node.name}"
            
        func_analysis = FunctionAnalysis(
            name=node.name,
            qualified_name=qual_name,
            parameters=parameters,
            decorators=decorators,
            source_location=self._get_location(node)
        )
        
        if self.current_class:
            self.current_class.methods.append(func_analysis)
        else:
            self.functions.append(func_analysis)
            
        self.function_stack.append(func_analysis)
        self.current_function = func_analysis
        self.function_strings.append({})
        self.function_vars.append({})
        
        self.generic_visit(node)
        
        self.function_vars.pop()
        self.function_strings.pop()
        self.function_stack.pop()
        self.current_function = self.function_stack[-1] if self.function_stack else None

    def visit_Assign(self, node: ast.Assign):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                if self.function_strings:
                    self.function_strings[-1][target_name] = node.value.value
                else:
                    self.module_strings[target_name] = node.value.value
            resolved_value = self._resolve_value(node.value)
            if resolved_value is not None:
                if self.function_vars:
                    self.function_vars[-1][target_name] = resolved_value
                else:
                    self.module_vars[target_name] = resolved_value
        self.generic_visit(node)

    def _resolve_value(self, node: ast.AST) -> Any:
        merged: Dict[str, Any] = dict(self.module_vars)
        if self.function_vars:
            merged.update(self.function_vars[-1])
        return resolve(node, merged, self.module_dicts)

    def visit_Return(self, node: ast.Return):
        if self.current_function:
            self.current_function.return_count += 1
        self.generic_visit(node)
        
    def _extract_targets(self, node: ast.AST) -> List[str]:
        targets = []
        if isinstance(node, ast.Name):
            targets.append(node.id)
        elif isinstance(node, ast.Tuple) or isinstance(node, ast.List):
            for elt in node.elts:
                targets.extend(self._extract_targets(elt))
        return targets

    def visit_For(self, node: ast.For):
        if self.current_function:
            self.current_function.control_flow.for_loops += 1

        # The iterable expression is evaluated before the loop body, so a DB
        # call used as the iterator (e.g. ``for row in cursor.fetchall()``) is
        # NOT inside the loop. Visit it before pushing the loop scope.
        self.visit(node.iter)

        targets = self._extract_targets(node.target)
        self.loop_stack.append(targets)

        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

        self.loop_stack.pop()

    def visit_AsyncFor(self, node: ast.AsyncFor):
        if self.current_function:
            self.current_function.control_flow.for_loops += 1

        self.visit(node.iter)

        targets = self._extract_targets(node.target)
        self.loop_stack.append(targets)

        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

        self.loop_stack.pop()

    def visit_While(self, node: ast.While):
        if self.current_function:
            self.current_function.control_flow.while_loops += 1

        # The test is evaluated outside the loop body.
        self.visit(node.test)

        self.loop_stack.append([])
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)
        self.loop_stack.pop()

    def _determine_sql_op(self, sql: Optional[str]) -> SqlOperation:
        if not sql:
            return "UNKNOWN"
        upper_sql = sql.strip().upper()
        if upper_sql.startswith("SELECT"):
            return "SELECT"
        if upper_sql.startswith("INSERT"):
            return "INSERT"
        if upper_sql.startswith("UPDATE"):
            return "UPDATE"
        if upper_sql.startswith("DELETE"):
            return "DELETE"
        return "OTHER"

    def _determine_db_op(self, call_name: str) -> DbOperationType:
        if call_name == "execute":
            return "EXECUTE"
        if call_name == "executemany":
            return "EXECUTEMANY"
        if call_name in ("fetchone", "fetchmany", "fetchall"):
            return "FETCH"
        if call_name == "commit":
            return "COMMIT"
        if call_name == "rollback":
            return "ROLLBACK"
        if call_name == "cursor":
            return "CURSOR"
        if call_name == "connect":
            return "CONNECT"
        return "UNKNOWN"
        
    def _extract_sql_string(self, arg: ast.AST) -> Optional[str]:
        # f-strings resolve to placeholder text in the shared resolver, which is
        # not a faithful SQL string; keep the historical behaviour of returning
        # None for them here.
        if not isinstance(arg, ast.JoinedStr):
            resolved = self._resolve_value(arg)
            if isinstance(resolved, str):
                return resolved
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        elif isinstance(arg, ast.Name):
            var_name = arg.id
            if self.function_strings and var_name in self.function_strings[-1]:
                return self.function_strings[-1][var_name]
            elif var_name in self.module_strings:
                return self.module_strings[var_name]
        return None

    def visit_Call(self, node: ast.Call):
        call_name = ""
        receiver = None
        
        if isinstance(node.func, ast.Name):
            call_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            call_name = node.func.attr
            if isinstance(node.func.value, ast.Name):
                receiver = node.func.value.id
            elif isinstance(node.func.value, ast.Attribute):
                receiver = node.func.value.attr
                
        if call_name:
            containing_func = self.current_function.qualified_name if self.current_function else None
            
            call_analysis = CallAnalysis(
                call_name=call_name,
                receiver=receiver,
                containing_function=containing_func,
                source_location=self._get_location(node)
            )
            
            if self.current_function:
                self.current_function.calls.append(call_analysis)
                
            db_op_type = self._determine_db_op(call_name)
            if db_op_type != "UNKNOWN":
                sql = None
                sql_template = None
                sql_op: SqlOperation = "UNKNOWN"
                param_deps = []
                
                # Check arguments
                if node.args:
                    sql = self._extract_sql_string(node.args[0])
                    if isinstance(node.args[0], ast.BinOp) and isinstance(node.args[0].op, ast.Mod):
                        sql_template = self._extract_sql_string(node.args[0].left)
                    if sql:
                        sql_op = self._determine_sql_op(sql)
                    
                    # Extract parameter dependencies from second argument if it's a name or list of names
                    if len(node.args) > 1:
                        params_arg = node.args[1]
                        if isinstance(params_arg, ast.Name):
                            param_deps.append(params_arg.id)
                        elif isinstance(params_arg, (ast.Tuple, ast.List)):
                            for elt in params_arg.elts:
                                if isinstance(elt, ast.Name):
                                    param_deps.append(elt.id)
                                    
                loop_vars = []
                for lv in self.loop_stack:
                    loop_vars.extend(lv)
                    
                db_op = DatabaseOperation(
                    call_name=call_name,
                    operation_type=db_op_type,
                    sql_operation=sql_op,
                    sql=sql,
                    sql_template=sql_template,
                    containing_function=containing_func,
                    inside_loop=len(self.loop_stack) > 0,
                    loop_variables=loop_vars,
                    parameter_dependencies=param_deps,
                    source_location=self._get_location(node)
                )
                
                self.database_operations.append(db_op)
                if self.current_function:
                    self.current_function.database_operations.append(db_op)
                
        self.generic_visit(node)


def analyze_source(source: str, file_path: Optional[str] = None) -> FileAnalysis:
    analysis = FileAnalysis(file_path=file_path or "")
    
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        analysis.parse_success = False
        analysis.parse_error = str(e)
        return analysis
        
    visitor = _FileVisitor(module_dicts=collect_module_dicts(tree))
    visitor.visit(tree)
    
    analysis.imports = visitor.imports
    analysis.classes = visitor.classes
    analysis.functions = visitor.functions
    analysis.database_operations = visitor.database_operations

    return analysis


def analyze_file(path: Path) -> FileAnalysis:
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except Exception as e:
        analysis = FileAnalysis(file_path=str(path))
        analysis.parse_success = False
        analysis.parse_error = str(e)
        return analysis
        
    return analyze_source(source, file_path=str(path))
