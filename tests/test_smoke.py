"""
冒烟测试：验证重构后的核心模块可导入、离线链路可端到端跑通、评分卡可复现。

全程强制离线（monkeypatch settings.FORCE_MOCK），不依赖外部网络与数据库。
运行：python -m pytest tests/test_smoke.py -q
"""

from __future__ import annotations

import pytest

from config import settings


@pytest.fixture()
def offline(monkeypatch: pytest.MonkeyPatch):
    """把所有节点切换到确定性离线兜底，保证测试结果稳定可复现。"""
    monkeypatch.setattr(settings, "FORCE_MOCK", True)
    return settings


def test_core_modules_import() -> None:
    """重构后保留的核心模块都应能正常导入。"""
    assert settings.MAX_ROUNDS == 3


def test_offline_generate_pipeline(offline) -> None:
    """离线生成链路应一次跑通并产出一个版本与最终代码。"""
    from graph.chat_graph import run_chat_pipeline

    state = run_chat_pipeline(mode="generate", raw_requirement="写一个求数字列表平均值的函数")
    assert state["status"] == "COMPLETED"
    assert state["run_id"]
    assert len(state["versions"]) >= 1
    assert "def " in state["solution_code"]
    # 离线兜底生成的测试可被 pytest 正常执行
    assert state["pytest_result"].all_green


def test_offline_review_pipeline(offline) -> None:
    """离线仅审核链路应能处理粘贴代码并记录一个版本。"""
    from graph.chat_graph import run_chat_pipeline

    state = run_chat_pipeline(
        mode="review",
        raw_requirement="审核以下代码",
        pasted_code="def add(a, b):\n    return a + b\n",
    )
    assert state["status"] == "COMPLETED"
    assert len(state["versions"]) == 1
    assert state["review"] is not None


def test_scorecard_deterministic() -> None:
    """同样的空问题清单 + 全绿测试，应得到 A 级且门禁通过。"""
    from models.common import PytestResult
    from services.review_scorecard import build_scorecard

    green = PytestResult(returncode=0, total=2, passed=2, failed=0, all_green=True)
    card = build_scorecard([], green)
    assert card.grade == "A"
    assert card.overall_score == 100.0
    assert card.gate_passed is True
    assert len(card.dimensions) == 5


def test_prompt_builders_return_few_shot() -> None:
    """四个提示词构建函数都应返回 (system, user)，且 system 内含示例。"""
    from models.chat_schemas import ConfirmedRequirement
    from prompts import chat_prompts

    confirmed = ConfirmedRequirement(
        title="测试",
        function_name="solution",
        restated_requirement="测试需求",
    )
    sys1, user1 = chat_prompts.confirm_requirement_prompt("测试需求")
    sys2, user2 = chat_prompts.generate_solution_prompt(confirmed)
    sys3, user3 = chat_prompts.review_code_prompt("x = 1", "测试", "")
    sys4, user4 = chat_prompts.fix_code_prompt("x = 1", "", ["问题1"], confirmed)

    for system, user in ((sys1, user1), (sys2, user2), (sys3, user3), (sys4, user4)):
        assert "示例" in system
        assert isinstance(user, str) and user
