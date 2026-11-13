"""需求中断与恢复的回归测试：问题能否挂起、回答能否续跑。"""

from graph import chat_graph
from graph.chat_graph import (
    has_pending_clarification,
    run_chat_pipeline,
    stream_chat_pipeline,
)
from models.chat_schemas import CodeReviewReport, ConfirmedRequirement
from services.chat_llm import ChatLLMService

QUESTION = "输入是数字列表还是单个数字？"


class _FakeLLM:
    """用确定性实现替换真实模型，专注验证图的控制流。"""

    offline = False
    build_report = staticmethod(ChatLLMService.build_report)

    def confirm_requirement(self, raw_text: str) -> ConfirmedRequirement:
        return ConfirmedRequirement(
            title="测试需求",
            function_name="echo_value",
            restated_requirement=raw_text,
            inputs=["value: int"],
            outputs="int",
            edge_cases=["value 为 None"],
            acceptance_criteria=["echo_value(1) == 1"],
            questions=[QUESTION],
        )

    def refine_requirement(self, raw_text, confirmed, answers):
        return confirmed.model_copy(update={"questions": []})

    def generate_solution(self, confirmed, knowledge_hits=None):
        return {
            "solution_code": "def echo_value(value):\n    return value\n",
            "test_code": (
                "from solution import echo_value\n\n"
                "def test_echo():\n    assert echo_value(1) == 1\n"
            ),
        }

    def review_code(self, solution, requirement, test_code="", hits=None, criteria=None):
        return CodeReviewReport(passed=True, items=[], summary="ok")

    def fix_code(self, *args, **kwargs):
        return {"solution_code": "def echo_value(value):\n    return value\n", "test_code": ""}


def test_interrupt_then_resume(monkeypatch) -> None:
    """第一次执行应停在 ask 节点并给出问题；提交回答后应跑完整个链路。"""
    monkeypatch.setattr(chat_graph, "ChatLLMService", _FakeLLM)

    thread = "test-thread-interrupt"
    events = list(stream_chat_pipeline("generate", "写个回声函数", thread_id=thread))
    assert events[-1]["type"] == "interrupt"
    assert events[-1]["value"]["questions"] == [QUESTION]
    assert has_pending_clarification(thread) is True

    resumed = list(
        stream_chat_pipeline(
            "generate",
            "写个回声函数",
            thread_id=thread,
            answers=[{"question": QUESTION, "answer": "整数"}],
            skip_interrupt=True,
        )
    )
    assert resumed[-1]["type"] == "final"
    state = resumed[-1]["state"]
    assert state["answers"][0]["answer"] == "整数"
    assert state["status"] == "COMPLETED"
    assert state["review"].passed is True
    assert has_pending_clarification(thread) is False


def test_no_question_runs_through(monkeypatch) -> None:
    """需求明确时不应中断，直接产出最终结果。"""
    monkeypatch.setattr(chat_graph, "ChatLLMService", _FakeLLM)
    monkeypatch.setattr(
        chat_graph.ChatLLMService,
        "confirm_requirement",
        lambda self, raw: ConfirmedRequirement(
            title="测试需求",
            function_name="echo_value",
            restated_requirement=raw,
            inputs=["value: int"],
            outputs="int",
            questions=[],
        ),
    )
    state = run_chat_pipeline("generate", "写个回声函数")
    assert state["status"] == "COMPLETED"
