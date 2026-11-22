"""
新增功能的结构化模型：需求确认、代码审核、测试与版本。

本文件定义“需求确定 → 代码生成 → 代码审核 → 测试 → 修复”这条新链路用到的
Pydantic 模型，与原 models/schemas.py 里的 MVP 模型并存，互不影响。

阅读顺序：ConfirmedRequirement（需求确认结果）
        → CodeReviewItem（单条审核问题）
        → CodeReviewReport（审核汇总）
        → ChatVersion（一个版本的代码+测试）
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

from models.common import PytestResult, Severity


# ---------------------------------------------------------------------------
# 1. 需求确认结果：LLM 把用户的口语化需求整理成结构化、可执行的需求
# ---------------------------------------------------------------------------
class ConfirmedRequirement(BaseModel):
    """需求确定结果：把模糊需求固化为明确、无歧义的结构化需求。"""

    title: str = Field(default="", description="一句话功能标题")
    function_name: str = Field(description="待实现函数名或项目入口名（LLM 给出规范命名）")
    restated_requirement: str = Field(description="用清晰自然语言重述的完整需求")
    inputs: list[str] = Field(default_factory=list, description="输入参数说明（含类型）")
    outputs: str = Field(default="", description="输出结果说明（含类型）")
    edge_cases: list[str] = Field(default_factory=list, description="边界与异常场景")
    constraints: list[str] = Field(default_factory=list, description="实现约束（如禁用 eval）")
    acceptance_criteria: list[str] = Field(default_factory=list, description="验收标准")
    assumptions: list[str] = Field(default_factory=list, description="需求不明确时 LLM 的假设")
    questions: list[str] = Field(default_factory=list, description="仍需用户澄清的问题（无则空）")
    language: str = Field(
        default="python",
        description="目标语言：python/java/c/html；html 可包含 CSS/JavaScript",
    )
    project_name: str = Field(default="", description="多文件项目的工程名")
    project_type: str = Field(
        default="function",
        description="交付形态：function（单函数/单文件）或 project（多文件小型项目）",
    )
    file_hints: list[str] = Field(
        default_factory=list,
        description="需求中明确要求的文件或目录（如 index.html、src/main.java）",
    )

    @field_validator("outputs", mode="before")
    @classmethod
    def _coerce_outputs(cls, value: Any) -> str:
        """模型偶尔把输出写成列表，统一拼成可读文本。"""
        if isinstance(value, list):
            return "；".join(str(item) for item in value)
        return "" if value is None else str(value)

    @field_validator(
        "title", "function_name", "language", "project_name", "project_type", mode="before"
    )
    @classmethod
    def _coerce_text(cls, value: Any) -> str:
        """标量字段统一转字符串，避免模型返回数字/列表时整条需求校验失败。"""
        if isinstance(value, list):
            return str(value[0]) if value else ""
        return "" if value is None else str(value)

    @field_validator(
        "inputs",
        "edge_cases",
        "constraints",
        "acceptance_criteria",
        "assumptions",
        "questions",
        "file_hints",
        mode="before",
    )
    @classmethod
    def _coerce_list(cls, value: Any) -> list[str]:
        """列表字段兼容单字符串，避免模型把数组写成一句话。"""
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]


class ProjectFile(BaseModel):
    """多文件项目中的一个文件（路径必须是项目内相对路径）。"""

    path: str = Field(description="项目内相对路径，如 src/main/java/App.java")
    content: str = Field(default="", description="文件完整内容")
    language: str = Field(
        default="",
        description="文件语言：python/java/c/html/css/javascript/markdown",
    )
    role: str = Field(
        default="source",
        description="文件角色：source/test/config/style/script/document/asset",
    )


# ---------------------------------------------------------------------------
# 2. 代码审核：单条问题 + 汇总报告
# ---------------------------------------------------------------------------
class CodeReviewItem(BaseModel):
    """单条代码审核问题（可定位到行，可执行修改）。"""

    view: str = Field(description="审核视角：spec/security/logic")
    severity: Severity = Field(description="严重等级：阻断/主要/次要")
    line_no: int | None = Field(default=None, description="所在行号")
    file_path: str = Field(default="solution.py", description="问题所在文件（项目内相对路径）")
    issue_type: str = Field(description="问题类型")
    description: str = Field(default="", description="问题描述")
    suggestion: str = Field(default="", description="具体修改建议")
    rule_code: str = Field(default="", description="工具规则码（如 E501/B101）")
    source: str = Field(default="llm", description="来源：tool/llm")
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="置信度 0~1：工具发现恒为 1，LLM 自行评估，低于阈值会被过滤",
    )
    item_id: str = Field(
        default_factory=lambda: f"rev_{uuid.uuid4().hex[:8]}", description="问题 ID"
    )


class ReviewDimension(BaseModel):
    """评分卡中的单个维度（固定 0-10，可复现）。"""

    key: str = Field(description="维度标识，如 correctness/security")
    name: str = Field(description="维度中文名")
    score: float = Field(description="本维度得分，0~10")
    weight: float = Field(default=1.0, description="在总分中的权重")
    findings: int = Field(default=0, description="本维度命中问题数")


class ReviewScorecard(BaseModel):
    """确定性审核评分卡：固定维度、总分、等级、门禁与优先行动。"""

    dimensions: list[ReviewDimension] = Field(default_factory=list, description="各维度得分")
    overall_score: float = Field(description="加权总分，0~100")
    grade: str = Field(description="字母等级 A~F")
    gate_passed: bool = Field(description="质量门禁是否通过（无阻断且测试全绿）")
    top_actions: list[str] = Field(
        default_factory=list, description="按优先级排序的改进动作，最多 3 条"
    )


class RagHit(BaseModel):
    """知识库检索命中的单个片段（带来源，便于前端展示与人工复核）。"""

    chunk_id: str = Field(description="片段编号，形如 security#2")
    doc: str = Field(description="来源文档文件名（不含扩展名）")
    doc_title: str = Field(default="", description="来源文档标题")
    section: str = Field(default="", description="片段所属小节标题")
    content: str = Field(default="", description="片段正文")
    score: float = Field(default=0.0, description="与查询的余弦相似度，0~1")


class CodeReviewReport(BaseModel):
    """代码审核汇总（三视角 + 结论 + 确定性评分卡）。"""

    report_id: str = Field(default_factory=lambda: f"cr_{uuid.uuid4().hex[:8]}")
    passed: bool = Field(description="是否通过（无阻断/主要问题）")
    items: list[CodeReviewItem] = Field(default_factory=list, description="全部问题")
    summary: str = Field(default="", description="结论摘要")
    counts: dict[str, int] = Field(default_factory=dict, description="各严重等级计数")
    scorecard: ReviewScorecard | None = Field(default=None, description="EEC 风格确定性评分卡")
    knowledge_hits: list[RagHit] = Field(
        default_factory=list, description="本次审核从知识库检索命中的规范片段"
    )


# ---------------------------------------------------------------------------
# 3. 一个版本：代码 + 测试 + 审核 + 测试结果
# ---------------------------------------------------------------------------
class ChatVersion(BaseModel):
    """一次生成/修复产出的版本（含代码、测试、审核与 pytest 结果）。"""

    version_no: int = Field(description="版本号，从 1 递增")
    round: int = Field(default=0, description="所属修复轮次")
    solution_code: str = Field(description="入口文件代码（兼容原单文件链路）")
    test_code: str = Field(default="", description="测试代码（Python 为 pytest，其它语言可为空）")
    language: str = Field(default="python", description="目标语言")
    project_name: str = Field(default="", description="项目名（单文件为空）")
    files: list[ProjectFile] = Field(default_factory=list, description="本版本全部项目文件")
    entry_file: str = Field(default="", description="入口文件相对路径")
    run_command: str = Field(default="", description="本地运行/构建命令")
    review: CodeReviewReport | None = Field(default=None, description="本版本审核报告")
    pytest: PytestResult | None = Field(default=None, description="本版本测试/编译检查结果")
    version_id: str = Field(
        default_factory=lambda: f"cv_{uuid.uuid4().hex[:8]}", description="版本 ID"
    )


# ---------------------------------------------------------------------------
# 4. 整条链路的最终输出（返回给前端）
# ---------------------------------------------------------------------------
class ChatPipelineResult(BaseModel):
    """新链路最终结果：需求、全部版本、最终代码、审核与测试。"""

    mode: str = Field(description="模式：generate（生成）/ review（仅审核）")
    run_id: str = Field(description="运行 ID")
    status: str = Field(description="最终状态：COMPLETED/BEST_EFFORT")
    requirement: ConfirmedRequirement | None = Field(
        default=None, description="确认后的需求（仅生成模式）"
    )
    versions: list[ChatVersion] = Field(default_factory=list, description="全部版本")
    final_code: str = Field(default="", description="最终入口文件代码")
    final_test: str = Field(default="", description="最终测试代码（流式中间态展示用）")
    final_files: list[ProjectFile] = Field(
        default_factory=list, description="最终项目的全部文件"
    )
    language: str = Field(default="python", description="最终项目目标语言")
    project_name: str = Field(default="", description="最终项目名")
    entry_file: str = Field(default="", description="最终项目入口文件")
    run_command: str = Field(default="", description="最终项目运行/构建命令")
    final_review: CodeReviewReport | None = Field(default=None, description="最终审核")
    final_pytest: PytestResult | None = Field(default=None, description="最终测试")
    repair_rounds: int = Field(default=0, description="实际修复轮次")
    raw_input: str = Field(default="", description="用户原始输入")

    def to_frontend(self) -> dict[str, Any]:
        """转成前端可直接 JSON 序列化的普通 dict（含枚举中文值）。"""
        return self.model_dump(mode="json")

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "ChatPipelineResult":
        """把图状态整理成结果对象；流式中间态与最终态共用同一口径。

        缺失字段一律取默认值：流式推送时每个节点完成即可序列化“当前进度”，
        前端据此增量渲染，不必等整条链路跑完。
        """
        return cls(
            mode=state.get("mode", "generate"),
            run_id=state.get("run_id", ""),
            status=state.get("status", "COMPLETED"),
            requirement=state.get("confirmed"),
            versions=state.get("versions", []),
            # final_code 只放“入口文件代码”，保持旧前端字段可用；
            # final_files 才是完整多文件项目。
            final_code=state.get("solution_code", ""),
            final_test=state.get("test_code", ""),
            final_files=state.get("project_files", []),
            language=state.get("language", "python"),
            project_name=state.get("project_name", ""),
            entry_file=state.get("entry_file", ""),
            run_command=state.get("run_command", ""),
            final_review=state.get("review"),
            final_pytest=state.get("pytest_result"),
            repair_rounds=state.get("round", 0),
            raw_input=state.get("raw_requirement", ""),
        )
