"""
chat_graph：用 LangGraph 构建新链路的状态图。

两条流程（入口按模式分流）：

  生成模式 generate：
      START → 需求确认 clarify → 代码生成 generate → 代码审核 review
            → 测试 test →（条件边）修复 fix → 回到 review 形成闭环 / 完成 / 超轮次兜底

  仅审核模式 review：
      START → 审核 review_only → 测试 test_only → 完成

节点只负责“调用服务 + 写状态”：大模型调用见 services/chat_llm.py，
文件落盘、静态工具与 pytest 见 services/chat_workspace.py。

对外提供两种执行方式：
    run_chat_pipeline    ：一次性执行，返回完整状态（便于测试）；
    stream_chat_pipeline ：逐节点产出阶段事件，最后给出完整状态（供 SSE 流式接口）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from config import settings
from models.chat_schemas import (
    ChatVersion,
    CodeReviewItem,
    CodeReviewReport,
    ConfirmedRequirement,
)
from models.common import PytestResult
from services.chat_llm import ChatLLMService
from services.chat_workspace import ChatWorkspace
from services.review_scorecard import build_scorecard, filter_low_confidence

# 各节点在前端展示的中文阶段名（用于流式事件）
STAGE_LABELS: dict[str, str] = {
    "clarify": "正在确认需求…",
    "generate": "正在生成代码…",
    "review": "正在审核代码…",
    "test": "正在运行测试…",
    "fix": "正在修复代码…",
    "finish": "处理完成",
    "best_effort": "已达最大修复轮次，保留当前最优版本",
    "review_only": "正在审核代码…",
    "test_only": "正在运行测试…",
    "finish_review": "处理完成",
}


def _assemble_report(
    tool_items: list[CodeReviewItem],
    llm_items: list[CodeReviewItem],
    pytest_result: PytestResult | None = None,
) -> CodeReviewReport:
    """合并工具/LLM 问题，过滤低置信度后构建报告并挂载确定性评分卡。

    :param pytest_result: 测试结果；审核节点尚未跑测试时传 None，
                          测试节点跑完后再用真实结果重建评分卡。
    """
    merged = filter_low_confidence(tool_items + llm_items)
    report = ChatLLMService.build_report(merged, "")
    report.scorecard = build_scorecard(merged, pytest_result)
    return report


# ===========================================================================
# 共享状态（黑板）
# ===========================================================================
class ChatGraphState(TypedDict, total=False):
    """新链路在各节点间传递的共享状态。"""

    mode: str  # generate / review
    run_id: str  # 运行 ID
    raw_requirement: str  # 用户原始需求
    pasted_code: str  # 仅审核模式：粘贴的代码
    pasted_test: str  # 仅审核模式：粘贴的测试（可空）
    confirmed: ConfirmedRequirement | None  # 确认后的结构化需求
    solution_code: str  # 当前实现代码
    test_code: str  # 当前测试代码
    review: CodeReviewReport | None  # 当前审核报告
    pytest_result: PytestResult | None  # 当前测试结果
    versions: list[ChatVersion]  # 全部版本
    round: int  # 当前修复轮次
    status: str  # 最终状态
    error: str  # 错误信息


# ===========================================================================
# 节点：生成模式
# ===========================================================================
def clarify_node(state: ChatGraphState) -> dict[str, Any]:
    """需求确认：把用户的原始需求交给 LLM 结构化。"""
    llm = ChatLLMService()
    raw = state["raw_requirement"]
    if llm.offline:  # 离线兜底：给出最小可用的结构化需求
        confirmed = ConfirmedRequirement(
            title="自定义函数",
            function_name="solution",
            restated_requirement=raw,
            inputs=["items"],
            outputs="处理结果",
            edge_cases=["None 输入", "空输入"],
            constraints=["禁用 eval/exec"],
        )
    else:
        confirmed = llm.confirm_requirement(raw)
    return {"confirmed": confirmed, "status": "RUNNING"}


def generate_node(state: ChatGraphState) -> dict[str, Any]:
    """代码生成：依据确认需求同时产出实现代码与测试代码。"""
    llm = ChatLLMService()
    confirmed = state["confirmed"]
    if llm.offline:  # 离线兜底：生成可通过语法检查的占位代码
        out = {
            "solution_code": f"def {confirmed.function_name}(items):\n    return items\n",
            "test_code": (
                f"from solution import {confirmed.function_name}\n\n"
                f"def test_basic():\n    assert {confirmed.function_name}([]) == []\n"
            ),
        }
    else:
        out = llm.generate_solution(confirmed)
    return {"solution_code": out["solution_code"], "test_code": out["test_code"]}


def review_node(state: ChatGraphState) -> dict[str, Any]:
    """代码审核：静态工具 + LLM 三视角，合并为一份审核报告。"""
    llm = ChatLLMService()
    ws = ChatWorkspace(state["run_id"])
    solution, test = state["solution_code"], state["test_code"]
    ws.write_files(solution, test)
    tool_items = ws.run_static()
    if llm.offline:
        llm_items: list[CodeReviewItem] = []
    else:
        req = state["confirmed"].restated_requirement
        llm_items = llm.review_code(solution, req, test).items
    # 此时还没跑 pytest，测试结果传 None
    report = _assemble_report(tool_items, llm_items, None)
    return {"review": report}


def test_node(state: ChatGraphState) -> dict[str, Any]:
    """测试：运行 pytest，用真实结果重建评分卡，并记录一个版本快照。"""
    ws = ChatWorkspace(state["run_id"])
    ws.write_files(state["solution_code"], state["test_code"])
    result = ws.run_pytest()
    base_report = state["review"]
    enriched = base_report.model_copy(
        update={"scorecard": build_scorecard(base_report.items, result)}
    )
    version = ChatVersion(
        version_no=state.get("round", 0) + 1,
        round=state.get("round", 0),
        solution_code=state["solution_code"],
        test_code=state["test_code"],
        review=enriched,
        pytest=result,
    )
    return {
        "pytest_result": result,
        "review": enriched,
        "versions": state.get("versions", []) + [version],
    }


def fix_node(state: ChatGraphState) -> dict[str, Any]:
    """定向修复：按当前审核问题最小化修改，轮次 +1。"""
    llm = ChatLLMService()
    new_round = state.get("round", 0) + 1
    if llm.offline:  # 离线兜底：保持原代码，避免闭环空转
        out = {"solution_code": state["solution_code"], "test_code": state["test_code"]}
    else:
        out = llm.fix_code(
            state["solution_code"],
            state["test_code"],
            state["review"].items,
            state["confirmed"],
        )
    return {
        "solution_code": out["solution_code"],
        "test_code": out["test_code"],
        "round": new_round,
    }


def finish_node(state: ChatGraphState) -> dict[str, Any]:
    """正常完成。"""
    return {"status": "COMPLETED"}


def best_effort_node(state: ChatGraphState) -> dict[str, Any]:
    """达到最大修复轮次仍未全绿，保留当前最优版本。"""
    return {"status": "BEST_EFFORT"}


# ===========================================================================
# 节点：仅审核模式
# ===========================================================================
def review_only_node(state: ChatGraphState) -> dict[str, Any]:
    """仅审核：对粘贴的代码做静态工具 + LLM 审核，不生成代码。"""
    llm = ChatLLMService()
    ws = ChatWorkspace(state["run_id"])
    code, test = state["pasted_code"], state.get("pasted_test", "")
    ws.write_files(code, test)
    tool_items = ws.run_static()
    if llm.offline:
        llm_items: list[CodeReviewItem] = []
    else:
        llm_items = llm.review_code(code, state["raw_requirement"], test).items
    report = _assemble_report(tool_items, llm_items, None)
    return {"review": report, "solution_code": code, "test_code": test, "status": "RUNNING"}


def test_only_node(state: ChatGraphState) -> dict[str, Any]:
    """仅审核模式的测试：有测试代码才跑 pytest，用真实结果重建评分卡并记录版本。"""
    ws = ChatWorkspace(state["run_id"])
    ws.write_files(state["pasted_code"], state.get("pasted_test", ""))
    result = ws.run_pytest()
    base_report = state["review"]
    enriched = base_report.model_copy(
        update={"scorecard": build_scorecard(base_report.items, result)}
    )
    version = ChatVersion(
        version_no=1,
        round=0,
        solution_code=state["pasted_code"],
        test_code=state.get("pasted_test", ""),
        review=enriched,
        pytest=result,
    )
    return {"pytest_result": result, "review": enriched, "versions": [version]}


def finish_review_node(state: ChatGraphState) -> dict[str, Any]:
    """仅审核模式完成。"""
    return {"status": "COMPLETED"}


# ===========================================================================
# 条件边
# ===========================================================================
def route_by_mode(state: ChatGraphState) -> str:
    """入口分流：按模式选择生成或仅审核流程。"""
    return "review" if state.get("mode") == "review" else "generate"


def route_after_test(state: ChatGraphState) -> str:
    """测试后路由：全绿则完成；有问题且未超轮次则修复；否则兜底。"""
    review = state.get("review")
    pytest_ok = bool(state.get("pytest_result") and state["pytest_result"].all_green)
    if review is not None and review.passed and pytest_ok:
        return "finish"
    if state.get("round", 0) < settings.MAX_ROUNDS:
        return "fix"
    return "best_effort"


# ===========================================================================
# 构建并编译图
# ===========================================================================
def build_chat_graph():
    """注册节点与边，返回编译后的可执行图。"""
    g = StateGraph(ChatGraphState)

    g.add_node("clarify", clarify_node)
    g.add_node("generate", generate_node)
    g.add_node("review", review_node)
    g.add_node("test", test_node)
    g.add_node("fix", fix_node)
    g.add_node("finish", finish_node)
    g.add_node("best_effort", best_effort_node)
    g.add_node("review_only", review_only_node)
    g.add_node("test_only", test_only_node)
    g.add_node("finish_review", finish_review_node)

    # 入口按模式分流
    g.add_conditional_edges(
        START,
        route_by_mode,
        {"generate": "clarify", "review": "review_only"},
    )

    # 生成模式主链路 + 修复闭环
    g.add_edge("clarify", "generate")
    g.add_edge("generate", "review")
    g.add_edge("review", "test")
    g.add_conditional_edges(
        "test",
        route_after_test,
        {"fix": "fix", "finish": "finish", "best_effort": "best_effort"},
    )
    g.add_edge("fix", "review")
    g.add_edge("finish", END)
    g.add_edge("best_effort", END)

    # 仅审核模式链路
    g.add_edge("review_only", "test_only")
    g.add_edge("test_only", "finish_review")
    g.add_edge("finish_review", END)

    return g.compile()


# 模块级单例：避免每次请求重复编译图
CHAT_GRAPH = build_chat_graph()


def _initial_state(
    mode: str, raw_requirement: str, pasted_code: str, pasted_test: str
) -> ChatGraphState:
    """组装图的初始状态。"""
    return {
        "mode": mode,
        "run_id": uuid.uuid4().hex[:12],
        "raw_requirement": raw_requirement,
        "pasted_code": pasted_code,
        "pasted_test": pasted_test,
        "round": 0,
        "versions": [],
    }


def run_chat_pipeline(
    mode: str, raw_requirement: str, pasted_code: str = "", pasted_test: str = ""
) -> ChatGraphState:
    """一次性执行新链路，返回完整状态（便于测试与脚本调用）。"""
    init = _initial_state(mode, raw_requirement, pasted_code, pasted_test)
    return CHAT_GRAPH.invoke(init)


def stream_chat_pipeline(
    mode: str, raw_requirement: str, pasted_code: str = "", pasted_test: str = ""
) -> Iterator[dict[str, Any]]:
    """逐节点产出阶段事件，最后产出一个完整状态事件。

    产出的事件有两种：
        {"type": "stage", "node": ..., "label": ..., "solution_code": ..., "round": ...}
        {"type": "final", "state": <完整状态>}
    """
    init = _initial_state(mode, raw_requirement, pasted_code, pasted_test)
    final_state: dict[str, Any] = dict(init)

    # stream_mode="updates"：每个节点执行完，返回该节点的增量更新
    for update in CHAT_GRAPH.stream(init, stream_mode="updates"):
        for node, patch in update.items():
            if not patch:
                continue
            # 节点返回的是该节点所改字段的完整新值，按键覆盖即可累积出最终状态
            final_state.update(patch)
            yield {
                "type": "stage",
                "node": node,
                "label": STAGE_LABELS.get(node, node),
                "solution_code": patch.get("solution_code"),
                "round": patch.get("round"),
            }

    yield {"type": "final", "state": final_state}
