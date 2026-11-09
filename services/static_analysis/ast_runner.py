"""
ast 静态检查：基于 Python 标准库 ast 的确定性检查（不依赖第三方）。

仅包含两条“在初始代码触发、在修复后不触发”的可靠规则，避免误报导致无法收敛：
1. 危险调用：eval / exec（阻断级，潜在代码执行风险）；
2. 缺失边界守卫：函数直接迭代参数或对参数取 len/set/sorted/sum 等，
   但函数开头缺少 None/空值守卫（主要级，空输入可能抛异常）。
"""

from __future__ import annotations

import ast
from pathlib import Path

from models.common import Severity, ToolFinding


def ast_tool_finding(
    rule_code: str, severity: Severity, line_no: int, message: str, file_path: str = "solution.py"
) -> ToolFinding:
    """构造一条 ast 工具发现。"""
    return ToolFinding(
        source="tool",
        tool="ast",
        rule_code=rule_code,
        severity=severity,
        file_path=file_path,
        line_no=line_no,
        message=message,
    )


# ---------------------------------------------------------------------------
# 规则 1：危险调用 eval / exec
# ---------------------------------------------------------------------------
def _check_dangerous_call(tree: ast.AST) -> list[ToolFinding]:
    """检测 eval / exec 调用。"""
    return [
        ast_tool_finding(
            "AST-EVAL",
            Severity.BLOCKER,
            node.lineno,
            f"使用了危险函数 {node.func.id}()，存在任意代码执行风险，应改为安全解析",
        )
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("eval", "exec")
        )
    ]


# ---------------------------------------------------------------------------
# 规则 2：缺失边界守卫
# ---------------------------------------------------------------------------
_BOUNDARY_FUNCS = ("len", "set", "sorted", "sum", "list", "tuple", "min", "max")


def _is_guard_test(test: ast.AST) -> bool:
    """判断 If 的测试是否为边界守卫（not X / X is None / isinstance(X) / 其 and-or 组合）。"""
    # and/or 组合：任一分支是守卫即可
    if isinstance(test, ast.BoolOp):
        return any(_is_guard_test(v) for v in test.values)
    # not X
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return True
    # X is None / X is not None
    if isinstance(test, ast.Compare) and any(
        isinstance(op, (ast.Is, ast.IsNot)) for op in test.ops
    ):
        return True
    # isinstance(X, ...)
    return (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Name)
        and test.func.id == "isinstance"
    )


def _has_guard(func: ast.FunctionDef) -> bool:
    """检查函数开头是否有边界守卫（跳过 docstring，只看第一个实际语句）。"""
    for stmt in func.body:
        # 跳过 docstring
        is_docstring = (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
        if is_docstring:
            continue
        # 第一个实际语句是守卫 If 即视为有守卫
        return isinstance(stmt, ast.If) and _is_guard_test(stmt.test)
    return False


def _uses_param_boundary(func: ast.FunctionDef, params: set[str]) -> bool:
    """判断函数是否直接对参数做迭代或取长度/集合等需要边界检查的操作。"""
    for node in ast.walk(func):
        # 直接迭代参数：for x in param
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Name) and node.iter.id in params:
            return True
        # 内建函数作用于参数：len(param) / set(param) / sorted(param) / sum(param)
        is_boundary_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _BOUNDARY_FUNCS
        )
        if is_boundary_call:
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id in params:
                    return True
    return False


def _check_missing_guard(tree: ast.AST) -> list[ToolFinding]:
    """检测缺失边界守卫的函数。"""
    findings: list[ToolFinding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            params = {a.arg for a in node.args.args}
            if _uses_param_boundary(node, params) and not _has_guard(node):
                findings.append(
                    ast_tool_finding(
                        "AST-GUARD",
                        Severity.MAJOR,
                        node.lineno,
                        "函数直接迭代或取参数长度/集合，但开头缺少 None/空值守卫，空输入可能抛异常",
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def analyze_source(source: str, file_path: str = "solution.py") -> list[ToolFinding]:
    """对单个源码文件运行 ast 检查。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        # 语法错误本身即阻断
        return [
            ast_tool_finding(
                "AST-SYNTAX", Severity.BLOCKER, e.lineno or 0, f"语法错误：{e.msg}", file_path
            )
        ]
    return _check_dangerous_call(tree) + _check_missing_guard(tree)


def run_ast(project_dir: Path | str) -> list[ToolFinding]:
    """对目录下所有 .py 文件运行 ast 检查。"""
    project_dir = Path(project_dir)
    findings: list[ToolFinding] = []
    for py_file in project_dir.rglob("*.py"):
        rel = py_file.relative_to(project_dir).as_posix()
        findings.extend(analyze_source(py_file.read_text(encoding="utf-8"), rel))
    return findings
