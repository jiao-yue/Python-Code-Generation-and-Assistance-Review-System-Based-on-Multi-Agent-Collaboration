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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from config import settings
from models.chat_schemas import (
    ChatPipelineResult,
    ChatVersion,
    CodeReviewItem,
    CodeReviewReport,
    ConfirmedRequirement,
    ProjectFile,
    RagHit,
    ReviewDimension,
    ReviewScorecard,
)
from models.common import PytestResult, Severity
from services.chat_llm import ChatLLMService
from services.chat_workspace import ChatWorkspace
from services.language_detect import (
    detect_file_hints,
    detect_project_type,
    normalize_language,
    safe_project_name,
)
from services.rag_retriever import build_query, rag_retriever
from services.review_scorecard import build_scorecard, filter_low_confidence

# 各节点在前端展示的中文阶段名（用于流式事件）
STAGE_LABELS: dict[str, str] = {
    "clarify": "正在确认需求…",
    "ask": "正在合并需求补充…",
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
    knowledge_hits: list[RagHit] | None = None,
) -> CodeReviewReport:
    """合并工具/LLM 问题，过滤低置信度后构建报告并挂载确定性评分卡。

    :param pytest_result: 测试结果；审核节点尚未跑测试时传 None，
                          测试节点跑完后再用真实结果重建评分卡。
    :param knowledge_hits: 本次审核检索命中的规范片段，随报告一起返回给前端。
    """
    # 三视角并行后同一问题可能被多个视角各报一次；先合并再评分，避免重复扣分。
    merged = _dedupe_review_items(tool_items + llm_items)
    merged = filter_low_confidence(merged)
    report = ChatLLMService.build_report(merged, "")
    report.scorecard = build_scorecard(merged, pytest_result)
    report.knowledge_hits = knowledge_hits or []
    return report


_SEVERITY_ORDER = {Severity.BLOCKER: 0, Severity.MAJOR: 1, Severity.MINOR: 2}


def _dedupe_review_items(items: list[CodeReviewItem]) -> list[CodeReviewItem]:
    """按“行号 + 问题类型”合并重复问题，保留更严重、描述更完整的一条。

    完全相同的根因被 spec/security/logic 三个视角重复报出时，评分卡会重复扣分，
    修复提示词也会变长。这里做确定性去重，不改变问题本身。多文件项目按
    “文件 + 行号 + 问题类型”去重，避免不同文件的同名行被误合并。
    """
    best: dict[tuple[str, int, str], CodeReviewItem] = {}
    for item in items:
        line_no = item.line_no or 0
        key = (item.file_path or "solution.py", line_no, item.issue_type.strip().lower())
        current = best.get(key)
        if current is None:
            best[key] = item
            continue
        new_rank = _SEVERITY_ORDER.get(item.severity, 9)
        old_rank = _SEVERITY_ORDER.get(current.severity, 9)
        if new_rank < old_rank or (
            new_rank == old_rank and len(item.description) > len(current.description)
        ):
            best[key] = item
    return sorted(
        best.values(),
        key=lambda item: (
            _SEVERITY_ORDER.get(item.severity, 9),
            item.file_path or "",
            item.line_no or 0,
            item.issue_type,
        ),
    )


def _retrieve_for_code(requirement: str, code: str) -> list[RagHit]:
    """按“需求 + 代码”检索知识库；检索层不可用时返回空列表，不影响主链路。"""
    return rag_retriever.search(build_query(requirement, code), top_k=settings.RAG_TOP_K)


def _generation_query(confirmed: ConfirmedRequirement) -> str:
    """把完整需求契约拼成生成阶段的检索查询。

    只检索 restated_requirement 会漏掉“禁用 eval”“文件路径”“空列表”等
    约束与边界，导致生成阶段拿不到对应规范；这里把契约中的关键字段一起拼入。
    """
    parts = [
        confirmed.restated_requirement,
        f"输入：{'；'.join(confirmed.inputs)}" if confirmed.inputs else "",
        f"输出：{confirmed.outputs}" if confirmed.outputs else "",
        f"边界：{'；'.join(confirmed.edge_cases)}" if confirmed.edge_cases else "",
        f"约束：{'；'.join(confirmed.constraints)}" if confirmed.constraints else "",
        f"验收：{'；'.join(confirmed.acceptance_criteria)}"
        if confirmed.acceptance_criteria
        else "",
        f"假设：{'；'.join(confirmed.assumptions)}" if confirmed.assumptions else "",
    ]
    return "\n".join(part for part in parts if part.strip())


def _pytest_review_item(
    result: PytestResult, require_tests: bool = True, language: str = "python"
) -> CodeReviewItem | None:
    """把 pytest/编译检查结果转成审核问题，保证修复节点能拿到真实的失败原因。

    pytest 是独立于 LLM 的确定性验证。若测试失败但审核报告没有对应条目，
    fix_node 的待修复问题就会为空，修复轮次会空转，甚至出现“审核通过但测试失败”。

    :param require_tests: 生成模式要求必须有测试；仅审核模式允许用户不附测试。
    :param language: 非 Python 项目跑的是编译/静态检查，提示文案不能写成 pytest 用例。
    """
    is_python = language == "python"
    if result.all_green:
        return None
    if result.total == 0:
        if not require_tests:
            return None
        return CodeReviewItem(
            view="test",
            severity=Severity.BLOCKER,
            issue_type="缺少可执行测试" if is_python else "项目检查未执行",
            description=(
                "pytest 没有收集到任何用例，无法验证实现是否满足需求。"
                if is_python
                else "项目检查没有执行到任何检查项，无法确认工程是否可以运行。"
            ),
            suggestion=(
                "补充覆盖正常路径、边界与异常场景的测试用例，并确保测试文件能被 pytest 正常收集。"
                if is_python
                else "确认项目包含入口文件与可编译源码，再重新运行编译/静态检查。"
            ),
            source="test",
            confidence=1.0,
        )

    detail = " ".join(result.traceback.split())
    if len(detail) > 600:
        detail = detail[:600] + "…"
    if is_python:
        description = f"{result.failed}/{result.total} 个测试用例未通过。"
        issue_type = "测试未通过"
        suggestion = (
            "按失败用例修正实现；若断言本身与需求不符，修正测试并保留能复现该缺陷的回归用例。"
        )
    else:
        description = f"{result.failed}/{result.total} 项项目检查未通过。"
        issue_type = "项目检查未通过"
        suggestion = "按检查输出修正编译、链接或静态检查错误，再重新执行项目检查。"
    if detail:
        description += f" 失败详情：{detail}"
    return CodeReviewItem(
        view="test",
        severity=Severity.BLOCKER,
        issue_type=issue_type,
        description=description,
        suggestion=suggestion,
        source="test",
        confidence=1.0,
    )


def _apply_pytest_result(
    base_report: CodeReviewReport,
    result: PytestResult,
    require_tests: bool = True,
    language: str = "python",
) -> CodeReviewReport:
    """把真实 pytest/项目检查结果合并进审核报告，并重建评分卡。

    测试失败时追加一条阻断级“测试未通过”问题；评分卡不重复给这条问题扣分，
    因为 build_scorecard 已经按 pytest 的失败比例扣过“测试充分性”维度。

    :param require_tests: 生成模式要求必须有测试；仅审核模式允许不附测试。
    """
    failure_item = _pytest_review_item(
        result, require_tests=require_tests, language=language
    )
    if failure_item is None:
        return base_report.model_copy(
            update={"scorecard": build_scorecard(base_report.items, result)}
        )

    items = [*base_report.items, failure_item]
    report = ChatLLMService.build_report(items, "")
    report.knowledge_hits = base_report.knowledge_hits
    # 评分卡故意用 base_report.items（不含这条测试失败项）：
    # build_scorecard 已经按 pytest 失败比例扣过“测试充分性”，
    # 再把 failure_item 放进去会重复扣分。前端仍能看到这条阻断问题。
    card = build_scorecard(base_report.items, result)
    action = f"【测试】{failure_item.issue_type}：{failure_item.suggestion}"
    report.scorecard = card.model_copy(update={"top_actions": [action, *card.top_actions][:3]})
    if language == "python":
        report.summary = (
            f"测试未通过：{result.failed}/{result.total} 个用例失败，需修复后重测。"
            if result.total
            else "未执行到任何测试用例，需补充测试后重测。"
        )
    else:
        report.summary = (
            f"项目检查未通过：{result.failed}/{result.total} 项检查失败，需修复后重新检查。"
            if result.total
            else "项目检查未执行到任何检查项，需确认工程文件完整后重试。"
        )
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
    # 评测专用：直接注入已确认需求与固定官方测试，跳过一次需求确认和大模型生成测试。
    # 正常网页链路不传这两个字段，因此不影响线上行为。
    confirmed_override: ConfirmedRequirement | None
    test_code_override: str
    questions: list[str]  # 需要用户补充回答的问题（为空表示可直接开工）
    answers: list[dict[str, str]]  # 用户对澄清问题的回答（问题 → 回答）
    skip_interrupt: bool  # 恢复执行时跳过再次中断
    solution_code: str  # 当前入口文件代码
    test_code: str  # 当前测试代码（Python pytest；其它语言可为空）
    project_files: list[ProjectFile]  # 当前多文件项目
    project_type: str  # function / project
    language: str  # python / java / c / html
    project_name: str  # 项目名
    entry_file: str  # 入口文件相对路径
    run_command: str  # 运行/构建命令
    review: CodeReviewReport | None  # 当前审核报告
    pytest_result: PytestResult | None  # 当前测试结果
    versions: list[ChatVersion]  # 全部版本
    round: int  # 当前修复轮次
    status: str  # 最终状态
    error: str  # 错误信息


# ===========================================================================
# 节点：生成模式
# ===========================================================================
def _files_from_state(state: ChatGraphState) -> list[ProjectFile]:
    """取当前项目文件；旧状态只有单段代码时自动包装成单文件。"""
    files = state.get("project_files") or []
    if files:
        return list(files)
    return [
        ProjectFile(
            path="solution.py",
            content=state.get("solution_code", ""),
            language="python",
            role="source",
        )
    ]


def _entry_code(files: list[ProjectFile], entry_file: str) -> str:
    """按入口路径取代码；入口缺失时取第一个文件，保证向后兼容。"""
    for item in files:
        if item.path == entry_file:
            return item.content
    return files[0].content if files else ""


def _offline_project(confirmed: ConfirmedRequirement) -> dict[str, Any]:
    """离线兜底项目：只保证语法与目录结构可运行，不冒充真实业务实现。"""
    language = confirmed.language
    name = confirmed.project_name or "offline_project"
    readme = ProjectFile(
        path="README.md",
        content=(
            f"# {name}\n\n离线模式生成的最小可运行工程，仅用于链路验证。\n\n"
            "运行方式见入口文件说明。\n"
        ),
        language="markdown",
        role="document",
    )
    if language == "java":
        files = [
            ProjectFile(
                path="src/Main.java",
                content=(
                    "public class Main {\n"
                    "    public static void main(String[] args) {\n"
                    '        System.out.println("离线模式：Java 项目骨架");\n'
                    "    }\n"
                    "}\n"
                ),
                language="java",
                role="source",
            ),
            readme,
        ]
        return {
            "project_name": name,
            "language": "java",
            "entry_file": "src/Main.java",
            "run_command": "javac -encoding UTF-8 -d build src/Main.java && java -cp build Main",
            "files": files,
        }
    if language == "c":
        files = [
            ProjectFile(
                path="main.c",
                content=(
                    "#include <stdio.h>\n\nint main(void) {\n"
                    '    printf("离线模式：C 项目骨架\\n");\n    return 0;\n}\n'
                ),
                language="c",
                role="source",
            ),
            readme,
        ]
        return {
            "project_name": name,
            "language": "c",
            "entry_file": "main.c",
            "run_command": "gcc -std=c11 main.c -o app",
            "files": files,
        }
    if language == "html":
        files = [
            ProjectFile(
                path="index.html",
                content=(
                    '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
                    '  <meta charset="UTF-8">\n  <title>离线项目</title>\n'
                    '  <link rel="stylesheet" href="styles.css">\n</head>\n<body>\n'
                    '  <main><h1>离线模式：HTML 项目骨架</h1>'
                    '<button id="btn">点击</button><p id="out">等待点击</p></main>\n'
                    '  <script src="app.js"></script>\n</body>\n</html>\n'
                ),
                language="html",
                role="source",
            ),
            ProjectFile(
                path="styles.css",
                content="body { font-family: sans-serif; margin: 40px; }\n",
                language="css",
                role="style",
            ),
            ProjectFile(
                path="app.js",
                content=(
                    "document.getElementById('btn').addEventListener('click', () => {\n"
                    "  document.getElementById('out').textContent = '已点击';\n"
                    "});\n"
                ),
                language="javascript",
                role="script",
            ),
            readme,
        ]
        return {
            "project_name": name,
            "language": "html",
            "entry_file": "index.html",
            "run_command": "直接用浏览器打开 index.html",
            "files": files,
        }
    files = [
        ProjectFile(
            path="app.py",
            content=(
                '"""离线模式的最小 Python 项目骨架。"""\n\n\n'
                "def add(a: int, b: int) -> int:\n"
                '    """返回两数之和。"""\n'
                "    return a + b\n\n\n"
                'if __name__ == "__main__":\n'
                '    print("离线模式：Python 项目骨架", add(1, 2))\n'
            ),
            language="python",
            role="source",
        ),
        ProjectFile(
            path="tests/test_app.py",
            content="from app import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n",
            language="python",
            role="test",
        ),
        readme,
    ]
    return {
        "project_name": name,
        "language": "python",
        "entry_file": "app.py",
        "run_command": "python app.py",
        "files": files,
    }


def clarify_node(state: ChatGraphState) -> dict[str, Any]:
    """需求确认：把原始需求整理成需求契约，并判断是否需要向用户提问。

    只有会改变实现结果的歧义才进入 ask 节点中断；能合理假设的直接开工。
    """
    llm = ChatLLMService()
    raw = state["raw_requirement"]
    override = state.get("confirmed_override")
    if override is not None:
        # 评测/脚本入口：需求已由数据集转换好，跳过确认调用，避免把基准题目
        # 再交给模型改写，也避免因 questions 触发不必要的中断。
        return {"confirmed": override, "questions": [], "status": "RUNNING"}
    if llm.offline:  # 离线兜底：给出最小可用的结构化需求，不打断用户
        language = normalize_language("", raw)
        hints = detect_file_hints(raw)
        project_type = detect_project_type(raw, language, hints)
        confirmed = ConfirmedRequirement(
            title="自定义项目" if project_type == "project" else "自定义函数",
            function_name="main" if project_type == "project" else "solution",
            restated_requirement=raw,
            inputs=["items"],
            outputs="处理结果",
            edge_cases=["None 输入", "空输入"],
            constraints=["禁用 eval/exec"],
            language=language,
            project_type=project_type,
            project_name=safe_project_name("自定义项目", language)
            if project_type == "project"
            else "",
            file_hints=hints,
        )
        questions: list[str] = []
    else:
        confirmed = llm.confirm_requirement(raw)
        # 恢复执行或明确跳过时不再中断；问题最多 3 条，避免审问式交互
        questions = [] if state.get("skip_interrupt") else list(confirmed.questions)[:3]
    return {"confirmed": confirmed, "questions": questions, "status": "RUNNING"}


def ask_node(state: ChatGraphState) -> dict[str, Any]:
    """中断询问：把待澄清问题交给前端，挂起图；用户回答后从本节点恢复。

    LangGraph 的 interrupt 会保存检查点并结束本次流；恢复时本节点重新执行，
    interrupt() 返回用户提交的回答，再合并进需求契约继续生成。
    """
    raw_answers = interrupt(
        {
            "type": "clarification",
            "questions": state.get("questions", []),
            "requirement": state["raw_requirement"],
        }
    )
    answers = _normalize_answers(raw_answers)
    llm = ChatLLMService()
    confirmed = state["confirmed"]
    if confirmed is None:
        raise ValueError("缺少已确认的需求，无法合并补充回答")
    if llm.offline:
        # 离线兜底：把问答直接补进需求描述，保证后续步骤仍有完整上下文
        answer_text = "；".join(item["answer"] for item in answers)
        confirmed = confirmed.model_copy(
            update={
                "restated_requirement": (
                    f"{confirmed.restated_requirement}\n补充说明：{answer_text}"
                ),
                "assumptions": [*confirmed.assumptions, *[a["answer"] for a in answers]],
            }
        )
    else:
        confirmed = llm.refine_requirement(state["raw_requirement"], confirmed, answers)
    return {"confirmed": confirmed, "answers": answers, "skip_interrupt": True}


def _normalize_answers(raw: Any) -> list[dict[str, str]]:
    """把前端提交的回答统一成 [{question, answer}]，过滤空回答。"""
    if isinstance(raw, dict) and "answers" in raw:
        raw = raw["answers"]
    pairs: list[dict[str, str]] = []
    if isinstance(raw, dict):
        for question, answer in raw.items():
            text = str(answer).strip()
            if text:
                pairs.append({"question": str(question).strip(), "answer": text})
        return pairs
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                answer = str(item.get("answer", "")).strip()
                question = str(item.get("question", "")).strip()
            else:
                answer, question = str(item).strip(), ""
            if answer:
                pairs.append({"question": question, "answer": answer})
        return pairs
    text = str(raw or "").strip()
    return [{"question": "", "answer": text}] if text else []


def generate_node(state: ChatGraphState) -> dict[str, Any]:
    """代码生成：先检索编码规范，再按需求语言产出单函数或多文件项目。"""
    llm = ChatLLMService()
    confirmed = state["confirmed"]
    # 用完整需求契约检索规范条目（输入、边界、约束、验收标准都会参与查询）
    knowledge_hits = rag_retriever.search(
        _generation_query(confirmed), top_k=settings.RAG_GENERATE_TOP_K
    )
    # project_type 是“交付形态开关”：project 走多文件项目，function 保持原来的
    # “一个 solution.py + 一个 test_solution.py”单函数链路。
    is_project = confirmed.project_type == "project"

    if is_project:
        if llm.offline:
            # 离线模式不调用大模型，生成一个能通过检查的最小项目，保证链路可演示。
            out = _offline_project(confirmed)
        else:
            out = llm.generate_project(confirmed, knowledge_hits)
        files: list[ProjectFile] = list(out["files"])
        entry = str(out.get("entry_file") or files[0].path)
        return {
            "project_files": files,
            "project_type": "project",
            "language": out.get("language") or confirmed.language,
            "project_name": out.get("project_name") or confirmed.project_name,
            "entry_file": entry,
            # solution_code 只保存“入口文件”的代码，供审核/展示兼容旧逻辑；
            # 真正完整的项目内容在 project_files 里。
            "run_command": out.get("run_command", ""),
            "solution_code": _entry_code(files, entry),
            # Python 项目的测试已作为 tests/test_*.py 放进 files，不再单独注入
            "test_code": "",
        }

    if llm.offline:  # 离线兜底：生成可通过语法检查的占位代码
        out = {
            "solution_code": f"def {confirmed.function_name}(items):\n    return items\n",
            "test_code": (
                f"from solution import {confirmed.function_name}\n\n"
                f"def test_basic():\n    assert {confirmed.function_name}([]) == []\n"
            ),
        }
    else:
        out = llm.generate_solution(confirmed, knowledge_hits)
    # 评测时官方测试是唯一判据，不允许模型用自己生成的测试替换。
    if state.get("test_code_override"):
        out["test_code"] = state["test_code_override"]
    files = [
        ProjectFile(path="solution.py", content=out["solution_code"], language="python"),
        ProjectFile(
            path="test_solution.py", content=out["test_code"], language="python", role="test"
        ),
    ]
    return {
        "solution_code": out["solution_code"],
        "test_code": out["test_code"],
        "project_files": files,
        "project_type": "function",
        "language": "python",
        "project_name": "",
        "entry_file": "solution.py",
        "run_command": "python -m pytest -q",
    }


def review_node(state: ChatGraphState) -> dict[str, Any]:
    """代码审核：检索规范 → 静态工具 + LLM 三视角 → 合并为一份审核报告。

    默认让 spec/security/logic 三次模型调用并行执行，每个视角只接收自己的
    指令；如果某次调用失败而其它视角成功，仍保留成功结果。全部失败时回退到
    单次串行 review_code，保证链路不会因为并行改造而中断。
    """
    llm = ChatLLMService()
    ws = ChatWorkspace(state["run_id"])
    files = _files_from_state(state)
    solution, test = state["solution_code"], state.get("test_code", "")
    requirement = state["confirmed"].restated_requirement
    language = state.get("language", "python")
    is_project = state.get("project_type") == "project"
    # RAG：先按代码风险信号检索规范片段，再把它作为审核依据注入提示词
    knowledge_hits = _retrieve_for_code(requirement, solution)
    ws.write_project(files, test)
    tool_items = ws.run_static()
    llm_items: list[CodeReviewItem] = []
    if not llm.offline:
        if is_project:
            if settings.REVIEW_PARALLEL:
                view_items: list[CodeReviewItem] = []
                errors: list[str] = []

                def _review_project_one(view: str) -> list[CodeReviewItem]:
                    try:
                        return llm.review_project_view(
                            view,
                            files,
                            requirement,
                            language,
                            knowledge_hits,
                            state["confirmed"].acceptance_criteria,
                        )
                    except Exception as exc:  # 单视角失败不丢其它视角结果
                        errors.append(f"{view}: {exc}")
                        return []

                workers = max(1, min(3, settings.REVIEW_MAX_WORKERS))
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="review") as pool:
                    futures = [
                        pool.submit(_review_project_one, view)
                        for view in ("spec", "security", "logic")
                    ]
                    for future in futures:
                        view_items += future.result()
                if errors:
                    print(f"[review] 部分视角失败，已保留成功结果：{errors}")
                llm_items = view_items
                if not view_items and errors:
                    llm_items = llm.review_project(
                        files,
                        requirement,
                        language,
                        knowledge_hits,
                        state["confirmed"].acceptance_criteria,
                    ).items
            else:
                llm_items = llm.review_project(
                    files,
                    requirement,
                    language,
                    knowledge_hits,
                    state["confirmed"].acceptance_criteria,
                ).items
        elif settings.REVIEW_PARALLEL and hasattr(llm, "review_code_view"):
            view_items: list[CodeReviewItem] = []
            errors: list[str] = []

            def _review_one(view: str) -> list[CodeReviewItem]:
                try:
                    return llm.review_code_view(
                        view,
                        solution,
                        requirement,
                        test,
                        knowledge_hits,
                        state["confirmed"].acceptance_criteria,
                    )
                except Exception as exc:  # 单个视角失败不应丢掉其它视角的结果
                    errors.append(f"{view}: {exc}")
                    return []

            workers = max(1, min(3, settings.REVIEW_MAX_WORKERS))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="review") as pool:
                futures = [pool.submit(_review_one, view) for view in ("spec", "security", "logic")]
                for future in futures:
                    view_items += future.result()
            if errors:
                print(f"[review] 部分视角失败，已保留成功结果：{errors}")
            if view_items:
                llm_items = view_items
            elif errors:
                # 三个视角全部失败时退回一次串行调用；仍失败则让异常向上抛，由接口展示。
                llm_items = llm.review_code(
                    solution,
                    requirement,
                    test,
                    knowledge_hits,
                    state["confirmed"].acceptance_criteria,
                ).items
        else:
            llm_items = llm.review_code(
                solution,
                requirement,
                test,
                knowledge_hits,
                state["confirmed"].acceptance_criteria,
            ).items
    # 此时还没跑 pytest，测试结果传 None；检索命中随报告返回前端
    report = _assemble_report(tool_items, llm_items, None, knowledge_hits)
    return {"review": report}


def test_node(state: ChatGraphState) -> dict[str, Any]:
    """测试/编译检查：按语言运行 pytest、javac、gcc 或 HTML 静态检查。

    用真实结果重建评分卡并记录版本快照；失败会变成阻断问题交给 fix_node。
    """
    ws = ChatWorkspace(state["run_id"])
    files = _files_from_state(state)
    language = state.get("language", "python")
    # 先把当前版本的文件写到独立目录，再用该语言对应的检查器执行。
    ws.write_project(files, state.get("test_code", ""))
    result = ws.run_project(language, state.get("test_code", ""))
    base_report = state["review"]
    # 把真实测试/编译结果并入报告：失败会追加阻断项，修复节点因此拿得到原因
    enriched = _apply_pytest_result(base_report, result, language=language)
    version = ChatVersion(
        version_no=state.get("round", 0) + 1,
        round=state.get("round", 0),
        solution_code=state["solution_code"],
        test_code=state.get("test_code", ""),
        language=language,
        project_name=state.get("project_name", ""),
        files=files,
        entry_file=state.get("entry_file", ""),
        run_command=state.get("run_command", ""),
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
    is_project = state.get("project_type") == "project"

    if is_project:
        files = _files_from_state(state)
        # 先接住当前项目元信息，离线修复时原样返回，在线修复时再用模型结果覆盖。
        # 这样后面的 return 只读 out_files/entry 等已赋值变量，不会出现“某个分支
        # 没有定义 out 却仍被条件表达式引用”的可读性问题。
        project_language = state.get("language", "python")
        project_name = state.get("project_name", "")
        run_command = state.get("run_command", "")
        if llm.offline:  # 离线兜底：保持原项目，避免闭环空转
            out_files = files
            entry = state.get("entry_file") or files[0].path
        else:
            out = llm.fix_project(files, state["review"].items, state["confirmed"])
            out_files = list(out["files"])
            entry = str(out.get("entry_file") or state.get("entry_file") or out_files[0].path)
            project_language = out.get("language", project_language)
            project_name = out.get("project_name", project_name)
            run_command = out.get("run_command", run_command)
        return {
            "project_files": out_files,
            "language": project_language,
            "project_name": project_name,
            "entry_file": entry,
            "run_command": run_command,
            "solution_code": _entry_code(out_files, entry),
            "test_code": state.get("test_code", ""),
            "round": new_round,
        }

    if llm.offline:  # 离线兜底：保持原代码，避免闭环空转
        out = {"solution_code": state["solution_code"], "test_code": state.get("test_code", "")}
    else:
        out = llm.fix_code(
            state["solution_code"],
            state.get("test_code", ""),
            state["review"].items,
            state["confirmed"],
        )
    test_code = state.get("test_code_override") or out["test_code"]
    # 评测时固定官方测试：模型即使改了测试，也必须换回原始测试再进入下一轮。
    return {
        "solution_code": out["solution_code"],
        "test_code": test_code,
        "project_files": [
            ProjectFile(path="solution.py", content=out["solution_code"], language="python"),
            ProjectFile(
                path="test_solution.py", content=test_code, language="python", role="test"
            ),
        ],
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
    """仅审核：检索规范 → 对粘贴的代码做静态工具 + LLM 审核，不生成代码。"""
    llm = ChatLLMService()
    ws = ChatWorkspace(state["run_id"])
    code, test = state["pasted_code"], state.get("pasted_test", "")
    requirement = state["raw_requirement"]
    knowledge_hits = _retrieve_for_code(requirement, code)
    ws.write_files(code, test)
    tool_items = ws.run_static()
    if llm.offline:
        llm_items: list[CodeReviewItem] = []
    else:
        llm_items = llm.review_code(code, requirement, test, knowledge_hits).items
    report = _assemble_report(tool_items, llm_items, None, knowledge_hits)
    return {"review": report, "solution_code": code, "test_code": test, "status": "RUNNING"}


def test_only_node(state: ChatGraphState) -> dict[str, Any]:
    """仅审核模式的测试：有测试代码才跑 pytest，用真实结果重建评分卡并记录版本。"""
    ws = ChatWorkspace(state["run_id"])
    ws.write_files(state["pasted_code"], state.get("pasted_test", ""))
    result = ws.run_pytest()
    base_report = state["review"]
    # 与生成模式保持一致：测试失败时把失败原因写进审核报告；
    # 仅审核模式允许用户不附测试，因此不把“没有测试”判为阻断。
    enriched = _apply_pytest_result(base_report, result, require_tests=False)
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


def route_after_clarify(state: ChatGraphState) -> str:
    """需求确认后分流：存在必须澄清的问题则中断询问，否则直接生成。"""
    return "ask" if state.get("questions") else "generate"


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
    """注册节点与边，返回编译后的可执行图（带内存检查点，支持中断恢复）。"""
    g = StateGraph(ChatGraphState)

    g.add_node("clarify", clarify_node)
    g.add_node("ask", ask_node)
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

    # 生成模式：需求确认 →（有疑问则中断询问）→ 生成 → 审核 → 测试 → 修复闭环
    g.add_conditional_edges(
        "clarify",
        route_after_clarify,
        {"ask": "ask", "generate": "generate"},
    )
    g.add_edge("ask", "generate")
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

    return g.compile(checkpointer=CHAT_CHECKPOINTER)


# 模块级检查点：保存中断现场，使同一 thread_id 能在下一次请求中恢复执行。
# 显式登记状态里用到的模型类型，避免反序列化时出现“未注册类型”告警。
CHAT_CHECKPOINTER = InMemorySaver(
    serde=JsonPlusSerializer(
        allowed_msgpack_modules=[
            ConfirmedRequirement,
            CodeReviewReport,
            CodeReviewItem,
            ChatVersion,
            PytestResult,
            RagHit,
            ReviewDimension,
            ReviewScorecard,
        ]
    )
)
CHAT_GRAPH = build_chat_graph()


def new_thread_id(conversation_id: str) -> str:
    """为一次生成运行分配独立 thread_id，避免多轮对话共用同一份检查点。"""
    return f"{conversation_id}#{uuid.uuid4().hex[:8]}"


def has_pending_clarification(thread_id: str) -> bool:
    """判断该 thread 是否停在中断节点上（服务重启后检查点丢失则返回 False）。"""
    if not thread_id:
        return False
    try:
        snapshot = CHAT_GRAPH.get_state({"configurable": {"thread_id": thread_id}})
    except Exception:
        return False
    return bool(snapshot.next)


def _initial_state(
    mode: str,
    raw_requirement: str,
    pasted_code: str,
    pasted_test: str,
    skip_interrupt: bool = False,
    confirmed_override: ConfirmedRequirement | None = None,
    test_code_override: str = "",
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
        "skip_interrupt": skip_interrupt,
        "confirmed_override": confirmed_override,
        "test_code_override": test_code_override,
    }


def run_chat_pipeline(
    mode: str,
    raw_requirement: str,
    pasted_code: str = "",
    pasted_test: str = "",
    thread_id: str = "",
    skip_interrupt: bool = False,
    confirmed_override: ConfirmedRequirement | None = None,
    test_code_override: str = "",
) -> ChatGraphState:
    """一次性执行新链路，返回完整状态（便于测试与脚本调用）。"""
    init = _initial_state(
        mode,
        raw_requirement,
        pasted_code,
        pasted_test,
        skip_interrupt,
        confirmed_override,
        test_code_override,
    )
    config = {"configurable": {"thread_id": thread_id or uuid.uuid4().hex[:12]}}
    return CHAT_GRAPH.invoke(init, config)


def stream_chat_pipeline(
    mode: str,
    raw_requirement: str,
    pasted_code: str = "",
    pasted_test: str = "",
    thread_id: str = "",
    answers: list[dict[str, str]] | None = None,
    skip_interrupt: bool = False,
    confirmed_override: ConfirmedRequirement | None = None,
    test_code_override: str = "",
) -> Iterator[dict[str, Any]]:
    """逐节点产出事件；遇到需求中断时产出 interrupt 事件并结束本次流。

    产出的事件有三种：
        {"type": "stage", "node": ..., "label": ..., "solution_code": ..., "round": ...,
         "partial": <当前进度的前端 payload>}
        {"type": "interrupt", "value": {...}, "thread_id": ...}
        {"type": "final", "state": <完整状态>}
    """
    thread = thread_id or uuid.uuid4().hex[:12]
    config = {"configurable": {"thread_id": thread}}
    init = _initial_state(
        mode,
        raw_requirement,
        pasted_code,
        pasted_test,
        skip_interrupt,
        confirmed_override,
        test_code_override,
    )
    final_state: dict[str, Any] = dict(init)
    # 有回答时用 Command 从断点恢复；否则从初始状态开始一次新运行
    payload: Any = Command(resume=answers) if answers is not None else init

    for update in CHAT_GRAPH.stream(payload, config, stream_mode="updates"):
        for node, patch in update.items():
            if node == "__interrupt__":
                value = patch[0].value if patch else {}
                yield {"type": "interrupt", "value": value, "thread_id": thread}
                return
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
                # 增量结果：本节点完成时的“当前进度”，供前端流式渲染；
                # 与最终 result 同一序列化口径，前端无需第二套拼装逻辑。
                "partial": ChatPipelineResult.from_state(final_state).to_frontend(),
            }

    yield {"type": "final", "state": final_state}
