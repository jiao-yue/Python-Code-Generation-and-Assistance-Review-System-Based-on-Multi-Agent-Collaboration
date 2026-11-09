"""
提示词模板（few-shot 示例模式）。

每个环节都提供一个“构建函数”，返回 (system, user) 两条文本：
    - system：角色 + 规则 + 一个完整的“输入 → 期望 JSON”示例，模型照着示例的格式与质量作答；
    - user  ：本次真正要处理的数据。

示例 JSON 用 json.dumps 生成，保证字段名、引号、缩进都合法，避免手写 JSON 出错。
"""

from __future__ import annotations

import json
from typing import Any

from models.chat_schemas import ConfirmedRequirement


def _pretty(obj: Any) -> str:
    """把 Python 对象转成带缩进、保留中文的 JSON 文本，方便嵌进提示词当示例。"""
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ===========================================================================
# 环节 1：需求确认
# ===========================================================================
_CONFIRM_EXAMPLE_INPUT = "写一个函数求数字列表的平均值，空列表的时候返回 0"

_CONFIRM_EXAMPLE_OUTPUT = {
    "title": "数字列表平均值",
    "function_name": "average_of_numbers",
    "restated_requirement": (
        "对一个数字列表求算术平均值；当列表为空或为 None 时返回 0，不抛出异常。"
    ),
    "inputs": ["numbers: list[float] 待求平均值的数字列表"],
    "outputs": "float：列表的算术平均值；空或 None 输入时返回 0",
    "edge_cases": [
        "numbers 为 None 时返回 0",
        "numbers 为空列表时返回 0",
        "正常列表返回各元素的算术平均值",
    ],
    "constraints": ["禁用 eval/exec", "仅使用 Python 标准库", "遵循 PEP8 规范"],
    "acceptance_criteria": [
        "average_of_numbers([1, 2, 3]) 等于 2",
        "average_of_numbers([]) 等于 0",
        "average_of_numbers(None) 等于 0",
    ],
    "assumptions": ["列表中的元素均为 int 或 float"],
    "questions": [],
}


def confirm_requirement_prompt(raw_text: str) -> tuple[str, str]:
    """需求确认环节的 (system, user)。"""
    system = (
        "你是资深软件需求分析师，负责把模糊、口语化的需求整理成无歧义、可实现的结构化需求。"
        "只输出一个 JSON 对象，不要输出任何解释性文字。\n"
        "规则：\n"
        "1. 用户未指定函数名时，依据功能给出规范的 snake_case 函数名；\n"
        "2. 明确输入参数（含类型）、输出（含类型）、边界与异常场景（None/空/非法输入等）；\n"
        "3. 给出实现约束（如禁用 eval/exec、PEP8、纯标准库）与可检验的验收标准；\n"
        "4. 不明确之处写进 assumptions；仅当无法合理假设时才在 questions 中提问。\n\n"
        "请严格参照下面示例的 JSON 字段与详尽程度作答。\n"
        "【示例输入】\n"
        f"{_CONFIRM_EXAMPLE_INPUT}\n"
        "【示例输出】\n"
        f"{_pretty(_CONFIRM_EXAMPLE_OUTPUT)}"
    )
    user = f"用户原始需求：\n{raw_text}\n\n请输出与示例结构完全一致的 JSON。"
    return system, user


# ===========================================================================
# 环节 2：代码生成
# ===========================================================================
_GENERATE_EXAMPLE_OUTPUT = {
    "solution_code": (
        "def average_of_numbers(numbers):\n"
        "    # 空列表或 None 直接返回 0，避免后续除零或迭代报错\n"
        "    if not numbers:\n"
        "        return 0\n"
        "    return sum(numbers) / len(numbers)\n"
    ),
    "test_code": (
        "from solution import average_of_numbers\n\n"
        "def test_average_normal():\n"
        "    assert average_of_numbers([1, 2, 3]) == 2\n\n"
        "def test_average_empty():\n"
        "    assert average_of_numbers([]) == 0\n\n"
        "def test_average_none():\n"
        "    assert average_of_numbers(None) == 0\n"
    ),
}


def generate_solution_prompt(confirmed: ConfirmedRequirement) -> tuple[str, str]:
    """代码生成环节的 (system, user)。"""
    system = (
        "你是资深 Python 工程师，严格按确认后的需求编写代码，只输出一个 JSON 对象。\n"
        "质量门槛：\n"
        "1. 函数名必须与需求中的 function_name 完全一致，不得改名；\n"
        "2. 覆盖全部 edge_cases：对 None/空/非法输入做防御，不抛未处理异常；\n"
        "3. 遵守 constraints 与 PEP8，不使用 eval/exec，仅使用需求允许的库；\n"
        "4. 同时编写 pytest 测试：覆盖正常路径、每个边界场景与验收标准，断言要具体；\n"
        "5. 测试通过 'from solution import 函数名' 导入，测试函数以 test_ 开头。\n\n"
        "请严格参照下面示例的 JSON 结构与代码风格作答。\n"
        "【示例输出】\n"
        f"{_pretty(_GENERATE_EXAMPLE_OUTPUT)}"
    )
    user = (
        "本次需求如下，请输出包含 solution_code 与 test_code 两个字段的 JSON：\n"
        f"{_pretty(confirmed.model_dump())}"
    )
    return system, user


# ===========================================================================
# 环节 3：代码审核
# ===========================================================================
_REVIEW_EXAMPLE_INPUT_CODE = "def run(cmd):\n    return eval(cmd)\n"

_REVIEW_EXAMPLE_OUTPUT = {
    "items": [
        {
            "view": "security",
            "severity": "critical",
            "line_no": 2,
            "issue_type": "危险函数调用",
            "description": "使用 eval() 直接执行字符串，存在任意代码执行的安全风险。",
            "suggestion": "去掉 eval，改用安全的解析方式或显式的分支映射。",
            "confidence": 0.95,
        }
    ],
    "summary": "发现 1 个阻断级安全问题，需要修复后再使用。",
}


def review_code_prompt(solution_code: str, requirement: str, test_code: str) -> tuple[str, str]:
    """代码审核环节的 (system, user)。"""
    system = (
        "你是严格的 Python 代码审核专家，以独立（新鲜）上下文从三个视角审核，"
        "只输出一个 JSON 对象：\n"
        "- spec（规范）：PEP8、命名、重复代码、可读性；\n"
        "- security（安全）：eval/exec、注入、不安全默认值、异常信息泄露、反序列化；\n"
        "- logic（逻辑）：是否满足需求、边界/空值/类型处理、分支与算法正确性，"
        "以及测试是否覆盖关键路径。\n"
        "只报告有把握、真实可定位、可修复的问题，不要泛泛而谈；没有问题时 items 输出空数组。\n"
        "每条问题给出 0~1 的 confidence，把握不足 0.6 的不要输出，以降低误报。\n"
        "severity 取值 critical/high/medium/low。\n\n"
        "请参照下面示例的 JSON 结构作答。\n"
        "【示例代码】\n"
        f"{_REVIEW_EXAMPLE_INPUT_CODE}\n"
        "【示例输出】\n"
        f"{_pretty(_REVIEW_EXAMPLE_OUTPUT)}\n"
        "代码完全没有问题时，输出形如："
        + _pretty({"items": [], "summary": "未发现阻断或主要问题，代码质量良好。"})
    )
    user = (
        f"需求：\n{requirement}\n\n"
        f"待审核代码：\n{solution_code}\n\n"
        f"测试代码：\n{test_code or '（无）'}\n\n"
        "请输出包含 items 与 summary 两个字段的 JSON。"
    )
    return system, user


# ===========================================================================
# 环节 4：定向修复
# ===========================================================================
_FIX_EXAMPLE_ORIGINAL = (
    "def average_of_numbers(numbers):\n    total = sum(numbers)\n    return total / len(numbers)\n"
)
_FIX_EXAMPLE_ISSUES = (
    "行2 [logic/主要] 缺失边界守卫：空列表会导致除零。建议：函数开头加 if not numbers: return 0"
)
_FIX_EXAMPLE_OUTPUT = {
    "solution_code": (
        "def average_of_numbers(numbers):\n"
        "    if not numbers:\n"
        "        return 0\n"
        "    total = sum(numbers)\n"
        "    return total / len(numbers)\n"
    ),
    "test_code": (
        "from solution import average_of_numbers\n\n"
        "def test_basic():\n"
        "    assert average_of_numbers([1, 2, 3]) == 2\n"
    ),
}


def fix_code_prompt(
    solution_code: str, test_code: str, issue_lines: list[str], confirmed: ConfirmedRequirement
) -> tuple[str, str]:
    """定向修复环节的 (system, user)。"""
    system = (
        "你是负责缺陷修复的 Python 工程师，只输出一个 JSON 对象。规则：\n"
        "1. 只针对列出的问题做最小化修改，禁止顺手重构或改动无关代码；\n"
        "2. 函数名必须保持不变，继续满足全部边界与约束；\n"
        "3. 问题在测试侧则修正 test_code，否则保持测试不退化。\n\n"
        "请参照下面示例，体会“只改必要部分”的修复方式。\n"
        "【示例原代码】\n"
        f"{_FIX_EXAMPLE_ORIGINAL}\n"
        "【示例待修复问题】\n"
        f"{_FIX_EXAMPLE_ISSUES}\n"
        "【示例输出】\n"
        f"{_pretty(_FIX_EXAMPLE_OUTPUT)}"
    )
    user = (
        f"必须保持的函数名：{confirmed.function_name}\n"
        f"当前代码：\n{solution_code}\n\n"
        f"当前测试：\n{test_code or '（无）'}\n\n"
        "待修复问题：\n"
        + "\n".join(issue_lines)
        + "\n\n请输出包含 solution_code 与 test_code 两个字段的 JSON。"
    )
    return system, user
