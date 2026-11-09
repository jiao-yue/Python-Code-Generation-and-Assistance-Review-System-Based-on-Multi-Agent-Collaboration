"""
脚本：按真实节点顺序验证“审核 → 测试 → 路由 → 修复”闭环（强制离线，不访问网络）。

运行：python tests/verify_chat_loop.py
"""

from __future__ import annotations

from config import settings

# 强制走确定性离线兜底，保证脚本可重复运行
settings.FORCE_MOCK = True

from graph.chat_graph import (  # noqa: E402  （需在设置开关后再导入图）
    fix_node,
    review_node,
    route_after_test,
    test_node,
)
from models.chat_schemas import ConfirmedRequirement  # noqa: E402

confirmed = ConfirmedRequirement(
    title="平均值",
    function_name="average_of_numbers",
    restated_requirement="求数字列表平均值，空列表返回 0",
    inputs=["numbers: list[float]"],
    outputs="float",
    edge_cases=["空列表返回 0", "None 返回 0"],
    constraints=["禁用 eval"],
)

buggy = (
    "def average_of_numbers(numbers):\n    total = sum(numbers)\n    return total / len(numbers)\n"
)
test = (
    "from solution import average_of_numbers\n\n"
    "def test_basic():\n"
    "    assert average_of_numbers([1, 2, 3]) == 2\n\n"
    "def test_empty():\n"
    "    assert average_of_numbers([]) == 0\n"
)

state = {
    "run_id": "looptest",
    "confirmed": confirmed,
    "solution_code": buggy,
    "test_code": test,
    "round": 0,
    "versions": [],
}

# 1) 审核 + 测试初始版本
state.update(review_node(state))
state.update(test_node(state))
print(
    "初始 pytest:",
    state["pytest_result"].passed,
    "/",
    state["pytest_result"].total,
    "green=",
    state["pytest_result"].all_green,
)

# 2) 条件路由：未全绿且未超轮次，应路由到 fix
decision = route_after_test(state)
print("路由结果:", decision)
assert decision == "fix", "失败用例应路由到修复"

# 3) 修复，再审核、再测试
state.update(fix_node(state))
state.update(review_node(state))
state.update(test_node(state))
print("versions =", len(state["versions"]))
# 手动节点流程没有经过 finish 节点，status 用 get 兜底，避免 KeyError
print("最终状态:", state.get("status", "手动流程已按 review→test→fix 顺序执行完毕"))
