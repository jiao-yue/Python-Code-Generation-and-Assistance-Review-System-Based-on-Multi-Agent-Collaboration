"""
ChatWorkspace：新链路的临时工程与静态检查/测试执行器。

职责：
1. 在 storage/generated 下为每次运行建立独立目录，写入 solution.py 与 test_solution.py；
2. 运行 Ruff / Bandit / ast 三个静态工具，并把 ToolFinding 转成统一的 CodeReviewItem；
3. 运行 pytest，返回结构化 PytestResult。

把“文件落盘 + 工具调用 + 结果转换”集中在这里，能让 LangGraph 节点保持简洁。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from config import settings
from models.chat_schemas import CodeReviewItem
from models.common import PytestResult, Severity, ToolFinding
from services.static_analysis.ast_runner import run_ast
from services.static_analysis.bandit_runner import run_bandit
from services.static_analysis.ruff_runner import run_ruff
from services.test_runner import TestRunner


class ChatWorkspace:
    """临时工程目录管理 + 静态检查 + 测试。"""

    def __init__(self, run_id: str) -> None:
        """按运行 ID 建立独立目录。"""
        self.run_id = run_id
        # storage/generated/chat_<run_id>，保证不同运行互不干扰
        self.dir: Path = settings.GENERATED_DIR / f"chat_{run_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.solution_path = self.dir / "solution.py"
        self.test_path = self.dir / "test_solution.py"

    def write_files(self, solution_code: str, test_code: str = "") -> None:
        """把实现代码与测试代码写入工程目录（UTF-8）。"""
        self.solution_path.write_text(solution_code, encoding="utf-8")
        if test_code:  # 没有测试代码时不生成测试文件
            self.test_path.write_text(test_code, encoding="utf-8")

    def run_static(self) -> list[CodeReviewItem]:
        """运行 Ruff/Bandit/ast，并把工具发现统一转成 CodeReviewItem。"""
        tool_findings: list[ToolFinding] = []
        # 三个工具各自捕获异常，单个工具失败不影响其他工具
        try:
            tool_findings += run_ruff(self.dir)
        except Exception as exc:
            print(f"[ChatWorkspace] Ruff 失败：{exc}")
        try:
            tool_findings += run_bandit(self.dir)
        except Exception as exc:
            print(f"[ChatWorkspace] Bandit 失败：{exc}")
        try:
            tool_findings += run_ast(self.dir)
        except Exception as exc:
            print(f"[ChatWorkspace] ast 失败：{exc}")
        return [self._to_review_item(f) for f in tool_findings]

    def run_pytest(self) -> PytestResult:
        """在工程目录运行 pytest；没有测试文件时返回空结果而非报错。"""
        if not self.test_path.exists():
            return PytestResult(
                returncode=0, all_green=False, traceback="未提供测试代码，跳过 pytest。"
            )
        return TestRunner().run(self.dir)

    def cleanup(self) -> None:
        """删除整个临时工程目录（保留结果由上层在内存中持有）。"""
        shutil.rmtree(self.dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # 工具发现 → 审核问题项
    # ------------------------------------------------------------------
    @staticmethod
    def _to_review_item(f: ToolFinding) -> CodeReviewItem:
        """把静态工具的 ToolFinding 转成 CodeReviewItem，并推断审核视角。"""
        if f.tool == "Ruff":
            view = "spec"
        elif f.tool == "Bandit":
            view = "security"
        else:  # ast：危险调用偏安全，其余偏逻辑
            view = "security" if f.severity == Severity.BLOCKER else "logic"
        return CodeReviewItem(
            view=view,
            severity=f.severity,
            line_no=f.line_no,
            issue_type=f.message[:40],
            description=f.message,
            suggestion="",
            rule_code=f.rule_code,
            source="tool",
        )
