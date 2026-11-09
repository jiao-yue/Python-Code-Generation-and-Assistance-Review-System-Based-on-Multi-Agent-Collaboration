"""
共享数据模型（新聊天链路的“公共积木”）。

这里只放各模块都会用到的三个基础结构，让其它文件的导入更清晰：
    1. Severity       —— 严重等级枚举（阻断 / 主要 / 次要）；
    2. ToolFinding    —— 静态工具（Ruff/Bandit/ast）发现的一条问题；
    3. PytestResult   —— pytest 运行结束后的结构化结果。

业务专属模型（需求、审核报告、版本等）见 models/chat_schemas.py。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    """问题严重等级（字符串枚举，可直接进行 JSON 序列化）。

    阻断：代码无法运行或存在安全隐患，必须处理；
    主要：逻辑、边界、异常类问题，应当处理；
    次要：命名、格式等可读性问题。
    """

    BLOCKER = "阻断"
    MAJOR = "主要"
    MINOR = "次要"


class ToolFinding(BaseModel):
    """静态检查工具发现的一条原始问题。

    不同工具（Ruff/Bandit/ast）的输出字段不同，统一转换成该模型后，
    才能与大模型的审核结果用同一套结构合并。
    """

    tool: str = Field(description="产生该问题的工具名，如 Ruff / Bandit / ast")
    rule_code: str = Field(description="工具规则码，如 E501、B307、AST-EVAL")
    severity: Severity = Field(description="映射后的统一严重等级")
    message: str = Field(description="问题的文字描述")
    file_path: str = Field(default="solution.py", description="所在文件名")
    line_no: int | None = Field(default=None, description="所在行号（无法定位时为空）")
    source: str = Field(default="tool", description="固定为 tool，标记来源是静态工具")


class PytestResult(BaseModel):
    """一次 pytest 执行的结果汇总。"""

    returncode: int = Field(description="pytest 进程返回码，0 表示全部通过")
    total: int = Field(default=0, description="测试用例总数")
    passed: int = Field(default=0, description="通过用例数")
    failed: int = Field(default=0, description="失败用例数（含 error）")
    duration: float = Field(default=0.0, description="运行耗时（秒）")
    traceback: str = Field(default="", description="失败用例的 traceback 文本")
    all_green: bool = Field(default=False, description="是否全部通过（有用例且 0 失败）")
