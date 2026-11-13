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
    # 生成提示词必须明确“示例只参考结构”，防止模型照搬平均值示例的业务
    assert "禁止照搬" in sys2


def test_generate_query_uses_full_contract() -> None:
    """生成阶段的检索查询应包含输入、边界、约束与验收标准。"""
    from graph.chat_graph import _generation_query
    from models.chat_schemas import ConfirmedRequirement

    confirmed = ConfirmedRequirement(
        title="读取文件",
        function_name="read_numbers",
        restated_requirement="从文本文件读取数字并求和",
        inputs=["path: str —— 文件路径"],
        outputs="float —— 数字之和",
        edge_cases=["空文件返回 0", "文件不存在抛出 FileNotFoundError"],
        constraints=["禁用 eval/exec", "只使用标准库"],
        acceptance_criteria=["read_numbers('empty.txt') == 0"],
    )
    query = _generation_query(confirmed)
    assert "文件路径" in query
    assert "空文件" in query
    assert "禁用 eval/exec" in query
    assert "read_numbers('empty.txt')" in query


def test_pytest_failure_becomes_blocking_review_item(offline) -> None:
    """审核通过但 pytest 失败时，必须补一条阻断项，修复节点才拿得到原因。"""
    from graph.chat_graph import review_node, route_after_test, test_node
    from models.chat_schemas import ConfirmedRequirement

    state = {
        "run_id": "pytestfail",
        "confirmed": ConfirmedRequirement(
            title="加法",
            function_name="add",
            restated_requirement="实现两个数的加法",
        ),
        "solution_code": "def add(a, b):\n    return a + b\n",
        "test_code": ("from solution import add\n\n\ndef test_add():\n    assert add(2, 2) == 5\n"),
        "round": 0,
        "versions": [],
    }
    state.update(review_node(state))
    assert state["review"].passed is True
    assert state["review"].items == []

    state.update(test_node(state))
    assert state["review"].passed is False
    failure_items = [item for item in state["review"].items if item.source == "test"]
    assert failure_items
    assert failure_items[0].severity.value == "阻断"
    assert "测试未通过" in failure_items[0].issue_type
    assert state["review"].scorecard is not None
    assert state["review"].scorecard.top_actions[0].startswith("【测试】")
    assert route_after_test(state) == "fix"


def test_test_source_item_is_not_filtered() -> None:
    """pytest 失败项是确定性结果，不能按 LLM 置信度阈值过滤掉。"""
    from models.chat_schemas import CodeReviewItem
    from models.common import Severity
    from services.review_scorecard import filter_low_confidence

    item = CodeReviewItem(
        view="test",
        severity=Severity.BLOCKER,
        issue_type="测试未通过",
        source="test",
        confidence=0.0,
    )
    assert filter_low_confidence([item]) == [item]


def test_review_only_without_tests_is_not_blocking() -> None:
    """仅审核模式允许不附测试，不能因为 total=0 就把审核判失败。"""
    from graph.chat_graph import _apply_pytest_result
    from models.chat_schemas import CodeReviewReport
    from models.common import PytestResult

    base = CodeReviewReport(passed=True, items=[], summary="未发现阻断问题")
    result = PytestResult(returncode=5, total=0, passed=0, failed=0, all_green=False)
    report = _apply_pytest_result(base, result, require_tests=False)
    assert report.passed is True
    assert report.items == []
    assert report.scorecard is not None


def test_workspace_removes_stale_test_file(tmp_path, monkeypatch) -> None:
    """修复轮次没有测试代码时，不能把上一版 test_solution.py 留在目录里。"""
    from config import settings
    from services.chat_workspace import ChatWorkspace

    monkeypatch.setattr(settings, "GENERATED_DIR", tmp_path)
    workspace = ChatWorkspace("stale-test")
    workspace.write_files("def f():\n    return 1\n", "def test_f():\n    assert True\n")
    assert workspace.test_path.exists()

    workspace.write_files("def f():\n    return 2\n", "")
    assert workspace.test_path.exists() is False


def test_test_runner_ignores_stale_junit(tmp_path) -> None:
    """上一轮的 JUnit 报告不能影响本轮统计。"""
    from services.test_runner import TestRunner

    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (tmp_path / "junit_report.xml").write_text(
        '<?xml version="1.0"?>'
        '<testsuites><testsuite tests="1" failures="1" errors="0" time="0"/></testsuites>',
        encoding="utf-8",
    )
    result = TestRunner(timeout=30).run(tmp_path)
    assert result.all_green is True
    assert result.failed == 0
    assert result.total == 1


def test_generate_solution_retries_incomplete_output(monkeypatch) -> None:
    """模型返回空字段时应带错误提示重试，而不是把空代码交给后续节点。"""
    from models.chat_schemas import ConfirmedRequirement
    from services.chat_llm import ChatLLMService

    confirmed = ConfirmedRequirement(
        title="测试",
        function_name="target",
        restated_requirement="返回输入值",
    )
    responses = iter(
        [
            {"solution_code": "", "test_code": ""},
            {
                "solution_code": "def target(value: int) -> int:\n    return value\n",
                "test_code": (
                    "from solution import target\n\n\n"
                    "def test_target() -> None:\n"
                    "    assert target(1) == 1\n"
                ),
            },
        ]
    )
    service = ChatLLMService()
    monkeypatch.setattr(service, "_chat_json", lambda system, user: next(responses))
    out = service.generate_solution(confirmed)
    assert "def target" in out["solution_code"]
    assert "test_target" in out["test_code"]


def test_fix_prompt_formats_missing_line_number() -> None:
    """测试失败项没有行号时，修复提示词不能出现“行None”。"""
    from models.chat_schemas import CodeReviewItem
    from models.common import Severity
    from services.chat_llm import ChatLLMService

    item = CodeReviewItem(
        view="test",
        severity=Severity.BLOCKER,
        issue_type="测试未通过",
        description="断言失败",
        suggestion="修正实现",
        source="test",
    )
    text = ChatLLMService._format_issue(item)
    assert "定位见代码" in text
    assert "行None" not in text
    assert "测试未通过" in text
