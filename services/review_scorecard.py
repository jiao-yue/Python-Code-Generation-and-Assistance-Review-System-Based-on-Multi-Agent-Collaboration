"""
EEC 风格的“确定性代码审核评分卡”引擎。

设计参考 GitHub 项目 affaan-m/everything-claude-code（EEC）的两个理念：
1. 评分必须**确定性、可复现**：同样的问题清单与测试结果，永远得到同样的分数，
   不允许模型临时拍脑袋加减分（对应 EEC 的 harness-audit 固定维度打分）。
2. **低置信度过滤**：LLM 自报的问题若置信度不足则丢弃，静态工具发现全部保留，
   以此降低误报（对应 EEC 的 >80% confidence 与 false-positive filtering）。

本模块只做两件事：
- filter_low_confidence：过滤低置信度的 LLM 问题；
- build_scorecard：把问题清单 + pytest 结果换算成 5 维度评分卡。
"""

from __future__ import annotations

from models.chat_schemas import (
    CodeReviewItem,
    ReviewDimension,
    ReviewScorecard,
)
from models.common import PytestResult, Severity

# LLM 问题保留的最低置信度；工具发现不受此限制（工具结果是确定性的）
CONFIDENCE_THRESHOLD = 0.6

# 五个固定维度的定义：key、中文名、权重（权重越大，对总分影响越大）
DIMENSION_DEFS = [
    ("correctness", "正确性", 1.2),
    ("security", "安全性", 1.2),
    ("style", "规范与可读性", 1.0),
    ("boundary", "边界与异常", 1.0),
    ("testing", "测试充分性", 0.8),
]

# 不同严重等级对维度得分的扣分（每个维度从满分 10 分起扣）
SEVERITY_PENALTY = {
    Severity.BLOCKER: 4.0,
    Severity.MAJOR: 2.0,
    Severity.MINOR: 1.0,
}

# 判定“边界与异常”问题时使用的关键词（出现在问题类型或描述里即归入边界维度）
BOUNDARY_KEYWORDS = ("边界", "空", "None", "异常", "默认", "越界", "零", "缺省", "null")


def filter_low_confidence(items: list[CodeReviewItem]) -> list[CodeReviewItem]:
    """过滤低置信度问题。

    - source == "tool"：静态工具发现，确定性结果，一律保留；
    - source == "llm"：仅当置信度达到阈值才保留。
    """
    return [
        item
        for item in items
        if item.source in {"tool", "test"} or item.confidence >= CONFIDENCE_THRESHOLD
    ]


def _dimension_of(item: CodeReviewItem) -> str:
    """把一条问题归类到五个维度之一。"""
    text = f"{item.issue_type} {item.description}"  # 合并类型与描述用于关键词判断
    # 与测试相关的问题，无论来自哪个视角都归入测试充分性
    if "测试" in text or "test" in text.lower():
        return "testing"
    if item.view == "security":
        return "security"
    if item.view == "spec":
        return "style"
    # logic 视角再细分为“正确性”和“边界与异常”
    if any(k.lower() in text.lower() for k in BOUNDARY_KEYWORDS):
        return "boundary"
    return "correctness"


def _clamp(value: float, low: float = 0.0, high: float = 10.0) -> float:
    """把分数限制在 [low, high] 区间。"""
    return max(low, min(high, value))


def build_scorecard(
    items: list[CodeReviewItem],
    pytest_result: PytestResult | None,
) -> ReviewScorecard:
    """根据问题清单与 pytest 结果构建确定性评分卡。"""
    # 每个维度先记录“当前得分”和“命中问题数”，初始满分 10、0 个问题
    scores = {key: 10.0 for key, _, _ in DIMENSION_DEFS}
    counts = {key: 0 for key, _, _ in DIMENSION_DEFS}

    # 逐条问题：归类到维度，按严重等级扣分
    for item in items:
        dim = _dimension_of(item)
        scores[dim] -= SEVERITY_PENALTY.get(item.severity, 1.0)
        counts[dim] += 1

    # 测试充分性维度额外结合 pytest 的真实执行结果
    if pytest_result is not None:
        if pytest_result.total == 0:
            # 完全没有用例：明显扣分，但不直接判 0（可能是只审核且未附测试）
            scores["testing"] -= 5.0
        elif not pytest_result.all_green:
            # 有用例但存在失败：先扣基础分，再按失败比例追加扣分
            scores["testing"] -= 3.0
            scores["testing"] -= (pytest_result.failed / pytest_result.total) * 2.0
        # 全绿则不额外扣分（测试相关问题的扣分已在上面处理）

    # 组装各维度结果，并统一夹到 0~10
    dimensions: list[ReviewDimension] = []
    weight_sum = 0.0
    weighted_sum = 0.0
    for key, name, weight in DIMENSION_DEFS:
        score = round(_clamp(scores[key]), 1)
        dimensions.append(
            ReviewDimension(
                key=key,
                name=name,
                score=score,
                weight=weight,
                findings=counts[key],
            )
        )
        weight_sum += weight
        weighted_sum += score * weight

    # 加权平均后换算成 0~100 的总分
    overall = round(weighted_sum / weight_sum * 10.0, 1)

    # 总分映射为字母等级 A~F
    if overall >= 90:
        grade = "A"
    elif overall >= 80:
        grade = "B"
    elif overall >= 70:
        grade = "C"
    elif overall >= 60:
        grade = "D"
    else:
        grade = "F"

    # 质量门禁：无阻断问题，且测试全绿（或没有测试）才算通过
    has_blocker = any(i.severity == Severity.BLOCKER for i in items)
    test_ok = pytest_result is None or pytest_result.all_green or pytest_result.total == 0
    gate_passed = not has_blocker and test_ok

    # 优先行动：取严重等级最高的前 3 条，转成可执行的动作描述
    severity_order = {Severity.BLOCKER: 0, Severity.MAJOR: 1, Severity.MINOR: 2}
    ordered = sorted(items, key=lambda i: severity_order.get(i.severity, 3))
    top_actions: list[str] = []
    for item in ordered[:3]:
        dim_name = next(
            (name for key, name, _ in DIMENSION_DEFS if key == _dimension_of(item)),
            "",
        )
        line = f"第 {item.line_no} 行" if item.line_no else "定位见代码"
        action = f"【{dim_name}】{item.issue_type}（{line}）：{item.suggestion or item.description}"
        top_actions.append(action)

    return ReviewScorecard(
        dimensions=dimensions,
        overall_score=overall,
        grade=grade,
        gate_passed=gate_passed,
        top_actions=top_actions,
    )
