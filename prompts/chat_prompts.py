"""
提示词模板（few-shot 示例模式，按真实研发项目的工程量设计）。

每个环节都提供一个“构建函数”，返回 (system, user) 两条文本：
    - system：角色 + 工程规则 + 一个完整的“输入 → 期望 JSON”示例；
    - user  ：本次真正要处理的数据。

设计原则：
1. 需求确认阶段先判断“能否直接开工”，只有会改变实现的歧义才提问，最多 3 条；
   能按工程惯例合理假设的写入 assumptions，避免用琐碎问题打断用户。
2. 生成阶段按需求契约逐条落地：类型注解、docstring、入参校验、边界处理、可执行测试。
3. 审核阶段以验收标准为基准，问题必须可定位、可复现、可修复，同一根因只报一次。
4. 修复阶段只改根因、保持公开接口不变，并补回归测试，禁止删断言换通过。
"""

from __future__ import annotations

import json
from typing import Any

from models.chat_schemas import ConfirmedRequirement, RagHit


def _pretty(obj: Any) -> str:
    """把 Python 对象转成带缩进、保留中文的 JSON 文本，方便嵌进提示词当示例。"""
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _knowledge_block(hits: list[RagHit] | None, max_chars: int = 600) -> str:
    """把 RAG 检索命中的知识片段渲染成提示词段落（无命中时返回空串）。"""
    if not hits:
        return ""
    lines = ["【知识库命中条目】（按相关度排序，来自本项目规范知识库）"]
    for index, hit in enumerate(hits, start=1):
        snippet = " ".join(hit.content.split())
        if len(snippet) > max_chars:
            snippet = snippet[:max_chars] + "…"
        lines.append(f"[{index}] {hit.doc} · {hit.section}（相关度 {hit.score:.0%}）\n{snippet}")
    return "\n".join(lines)


def _answers_block(pairs: list[dict[str, str]]) -> str:
    """把用户对澄清问题的回答渲染成“问题 → 回答”列表。"""
    return "\n".join(
        f"- 问题：{pair.get('question', '').strip()}\n  回答：{pair.get('answer', '').strip()}"
        for pair in pairs
        if pair.get("answer", "").strip()
    )


# ===========================================================================
# 环节 1：需求确认
# ===========================================================================
_CONFIRM_EXAMPLE_INPUT = "写一个函数求数字列表的平均值，空列表的时候返回 0"

_CONFIRM_EXAMPLE_OUTPUT = {
    "title": "数字列表平均值计算",
    "function_name": "average_of_numbers",
    "restated_requirement": (
        "实现 average_of_numbers(numbers)，计算数字序列的算术平均值；"
        "numbers 为 None 或空列表时返回 0.0；元素含非数字时抛出 TypeError。"
    ),
    "inputs": ["numbers: Sequence[int | float] | None —— 待计算的数字序列"],
    "outputs": "float —— 算术平均值；None 或空序列返回 0.0",
    "edge_cases": [
        "numbers 为 None",
        "numbers 为空列表",
        "numbers 只有一个元素",
        "numbers 含负数",
        "numbers 含非数字元素",
    ],
    "constraints": [
        "仅使用 Python 标准库",
        "禁用 eval/exec",
        "不修改入参",
        "时间复杂度 O(n)",
    ],
    "acceptance_criteria": [
        "average_of_numbers([1, 2, 3]) == 2.0",
        "average_of_numbers([]) == 0.0",
        "average_of_numbers(None) == 0.0",
        "average_of_numbers([1, 'a']) 抛出 TypeError",
    ],
    "assumptions": [
        "调用方传入的是可重复遍历的序列，而不是一次性生成器",
        "结果统一按 float 返回",
    ],
    "questions": [],
}


def confirm_requirement_prompt(raw_text: str) -> tuple[str, str]:
    """需求确认环节的 (system, user)。"""
    system = (
        "你是资深 Python 研发工程师兼需求分析师，负责把用户需求整理成可直接实现、"
        "可验收的需求契约。只输出一个 JSON 对象，不要输出解释性文字。\n"
        "工作方法：\n"
        "1. 先判断需求是否足以开工：功能目标、输入类型与范围、输出、边界、约束、验收标准；\n"
        "2. 能按工程惯例合理假设且不改变核心行为的，写入 assumptions，不打断用户；\n"
        "3. 只有会改变实现结果的歧义才提问，最多 3 条，且每条都能用一句话回答；\n"
        "4. edge_cases 至少覆盖 None、空输入、非法类型、数值/序列边界四类；\n"
        "5. acceptance_criteria 必须可执行、可断言，写成“函数(输入) 等于/抛出 预期”的形式；\n"
        "6. constraints 写明依赖范围、性能要求、禁用项与兼容性要求。\n\n"
        "必须提问的典型情况：\n"
        "- 输入是字符串、数字列表还是文件路径，会直接改变实现方式；\n"
        "- 非法输入应抛异常还是返回默认值，需求未说明且两种都合理；\n"
        "- 用户指定了第三方库或运行环境，但缺少版本/平台等必要信息。\n"
        "不要提问的情况：函数命名、注释风格、常规边界处理，"
        "这些按工程惯例决定并写进 assumptions。\n\n"
        "请严格参照下面示例的 JSON 字段与详尽程度作答。\n"
        "【示例输入】\n"
        f"{_CONFIRM_EXAMPLE_INPUT}\n"
        "【示例输出】\n"
        f"{_pretty(_CONFIRM_EXAMPLE_OUTPUT)}"
    )
    user = f"用户原始需求：\n{raw_text}\n\n请输出与示例结构完全一致的 JSON。"
    return system, user


# ===========================================================================
# 环节 1.5：用户补充回答后，合并需求
# ===========================================================================
def refine_requirement_prompt(
    raw_text: str,
    confirmed: ConfirmedRequirement,
    answers: list[dict[str, str]],
) -> tuple[str, str]:
    """把用户对澄清问题的回答合并进已确认需求，返回同样的 JSON 结构。"""
    system = (
        "你是资深 Python 研发工程师兼需求分析师。用户已经回答了需求澄清问题，"
        "请把回答合并进需求契约，只输出一个 JSON 对象。规则：\n"
        "1. 保持字段结构与输入完全一致，不要新增字段；\n"
        "2. 回答改变的行为要同步更新 restated_requirement、inputs、outputs、edge_cases、"
        "acceptance_criteria；\n"
        "3. 已解决的疑问从 questions 中删除；仍有未解决且影响实现的疑问才保留；\n"
        "4. 不臆造用户没有表达的需求，不确定的仍写进 assumptions。\n\n"
        "请参照下面示例的字段与详尽程度作答。\n"
        "【示例输出】\n"
        f"{_pretty(_CONFIRM_EXAMPLE_OUTPUT)}"
    )
    user = (
        f"原始需求：\n{raw_text}\n\n"
        f"上一版需求契约：\n{_pretty(confirmed.model_dump())}\n\n"
        f"用户补充回答：\n{_answers_block(answers)}\n\n"
        "请输出合并后的完整需求 JSON。"
    )
    return system, user


# ===========================================================================
# 环节 2：代码生成
# ===========================================================================
_GENERATE_EXAMPLE_OUTPUT = {
    "solution_code": (
        "from collections.abc import Sequence\n\n\n"
        "def average_of_numbers(numbers: Sequence[float] | None) -> float:\n"
        '    """计算数字序列的算术平均值。\n\n'
        "    :param numbers: 数字序列；None 或空序列视为没有数据\n"
        "    :return: 算术平均值；没有数据时返回 0.0\n"
        '    """\n'
        "    if numbers is None or len(numbers) == 0:\n"
        "        return 0.0\n"
        "    for value in numbers:\n"
        "        if isinstance(value, bool) or not isinstance(value, (int, float)):\n"
        "            raise TypeError(f'元素必须是数字，实际为 {type(value).__name__}')\n"
        "    return float(sum(numbers) / len(numbers))\n"
    ),
    "test_code": (
        "import pytest\n\n"
        "from solution import average_of_numbers\n\n\n"
        "def test_normal_sequence() -> None:\n"
        "    assert average_of_numbers([1, 2, 3]) == 2.0\n\n\n"
        "def test_empty_list() -> None:\n"
        "    assert average_of_numbers([]) == 0.0\n\n\n"
        "def test_none_input() -> None:\n"
        "    assert average_of_numbers(None) == 0.0\n\n\n"
        "def test_single_element() -> None:\n"
        "    assert average_of_numbers([5]) == 5.0\n\n\n"
        "def test_negative_numbers() -> None:\n"
        "    assert average_of_numbers([-2, -4]) == -3.0\n\n\n"
        "def test_invalid_element_raises() -> None:\n"
        "    with pytest.raises(TypeError):\n"
        "        average_of_numbers([1, 'a'])\n"
    ),
}


def generate_solution_prompt(
    confirmed: ConfirmedRequirement, knowledge_hits: list[RagHit] | None = None
) -> tuple[str, str]:
    """代码生成环节的 (system, user)；knowledge_hits 为 RAG 检索到的规范条目。"""
    knowledge_block = _knowledge_block(knowledge_hits)
    knowledge_rule = (
        f"6. 下面这些规范是本次检索到的相关条款，写代码与测试时必须遵守：\n{knowledge_block}\n"
        if knowledge_block
        else ""
    )
    system = (
        "你是资深 Python 工程师，按需求契约实现代码并配套可执行测试，只输出一个 JSON 对象。\n"
        "工程要求：\n"
        "1. 函数名与 function_name 完全一致，签名覆盖 inputs 中的参数与类型；\n"
        "2. 为公开函数写 docstring（参数含义、返回值、可能抛出的异常），添加类型注解；\n"
        "3. 入参先校验：None、空输入、类型错误、数值/序列边界，按 edge_cases 逐条处理；\n"
        "4. 不用可变对象作默认参数，不修改入参，不读写全局状态，不打印调试输出；\n"
        "5. 除需求明确要求，不访问网络、文件、数据库或执行系统命令；不使用 eval/exec；\n"
        "6. 测试覆盖正常路径、每个 edge_case 与验收标准；异常用 pytest.raises(具体类型)；\n"
        "7. 测试通过 'from solution import 函数名' 导入，不 mock 被测函数，不依赖时间/随机/网络。\n"
        + knowledge_rule
        + "\n【示例使用规则】示例只演示 JSON 字段、代码粒度与测试覆盖方式；"
        "禁止照搬示例中的函数名、参数名、业务场景和边界条件，"
        "必须按本次需求契约重新实现。\n"
        "请严格参照下面示例的 JSON 结构、代码风格与测试覆盖度作答。\n"
        "【示例输出】\n"
        f"{_pretty(_GENERATE_EXAMPLE_OUTPUT)}"
    )
    user = (
        "本次需求契约如下，请输出包含 solution_code 与 test_code 两个字段的 JSON。"
        "代码中不要出现 markdown 围栏，测试必须可直接运行：\n"
        f"{_pretty(confirmed.model_dump())}"
    )
    return system, user


# ===========================================================================
# 环节 3：代码审核
# ===========================================================================
_REVIEW_EXAMPLE_INPUT_CODE = (
    "from collections.abc import Sequence\n\n\n"
    "def average_of_numbers(numbers: Sequence[float] | None) -> float:\n"
    "    if not numbers:\n"
    "        return 0\n"
    "    return sum(numbers) / len(numbers)\n"
)

_REVIEW_EXAMPLE_OUTPUT = {
    "items": [
        {
            "view": "logic",
            "severity": "high",
            "line_no": 6,
            "issue_type": "空输入与类型未校验",
            "description": (
                "numbers 为 None 时被 `if not numbers` 当作空值返回 0，但需求要求返回 0.0；"
                "当传入字符串时会按字符求和并抛出 TypeError，错误信息不可读。"
            ),
            "suggestion": (
                "在函数开头显式判断 `numbers is None or len(numbers) == 0`，"
                "并逐个校验元素类型，非数字时抛出 TypeError。"
            ),
            "confidence": 0.95,
        },
        {
            "view": "spec",
            "severity": "medium",
            "line_no": 4,
            "issue_type": "缺少返回类型契约",
            "description": (
                "空输入返回 int 0，正常路径返回 float，返回值类型不稳定，"
                "与 outputs 声明的 float 不一致。"
            ),
            "suggestion": "统一返回 float，空输入写 `return 0.0`。",
            "confidence": 0.9,
        },
    ],
    "summary": "发现 1 个主要问题与 1 个次要问题：输入契约与返回类型不符合需求，需修复后重测。",
}


def review_code_prompt(
    solution_code: str,
    requirement: str,
    test_code: str,
    knowledge_hits: list[RagHit] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> tuple[str, str]:
    """代码审核环节的 (system, user)；knowledge_hits 为 RAG 检索到的规范条目。"""
    knowledge_block = _knowledge_block(knowledge_hits)
    knowledge_section = (
        f"审核时以下面的知识库条款作为判定依据：\n{knowledge_block}\n" if knowledge_block else ""
    )
    criteria_block = ""
    if acceptance_criteria:
        criteria_block = (
            "【验收标准（逐条核对，未满足的必须报出）】\n"
            + "\n".join(f"- {item}" for item in acceptance_criteria)
            + "\n"
        )
    system = (
        "你是严格的 Python 代码审核专家，以独立上下文审核实现与测试，只输出一个 JSON 对象。\n"
        "审核视角：\n"
        "- spec：PEP8、命名、类型注解、docstring、重复代码与可维护性；\n"
        "- security：eval/exec、注入、反序列化、密钥泄露、异常信息泄露、不安全文件/网络操作；\n"
        "- logic：需求与验收标准是否满足、边界/空值/类型处理、算法正确性、返回值契约；\n"
        "- test：测试是否覆盖正常路径、每个边界与异常、断言是否具体可失败。\n"
        "报告规则：\n"
        "1. 每条问题必须给出真实 line_no、触发条件、后果与可执行的修改建议；\n"
        "2. 同一根因只报一次，不重复罗列表面现象；\n"
        "3. severity 口径：critical=安全风险或必然崩溃；high=功能错误或关键边界缺失；"
        "medium=可维护性/契约不一致；low=风格细节；\n"
        "4. 只报有证据的问题，不猜测；纯风格偏好不报，除非违反 PEP8；\n"
        "5. 每条给出 0~1 的 confidence，把握不足 0.6 的不输出；没有问题时 items 为空数组。\n"
        + knowledge_section
        + criteria_block
        + "\n请参照下面示例的 JSON 结构与问题粒度作答。\n"
        "【示例代码】\n"
        f"{_REVIEW_EXAMPLE_INPUT_CODE}\n"
        "【示例输出】\n"
        f"{_pretty(_REVIEW_EXAMPLE_OUTPUT)}\n"
        "代码完全符合需求时，输出形如："
        + _pretty(
            {"items": [], "summary": "实现与测试均满足需求契约与验收标准，未发现阻断或主要问题。"}
        )
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
    "行2 [logic/high] 缺失边界守卫：空列表会导致 ZeroDivisionError，None 会在 sum 中报 TypeError。"
    "建议：开头加 `if numbers is None or len(numbers) == 0: return 0.0`"
)
_FIX_EXAMPLE_OUTPUT = {
    "solution_code": (
        "from collections.abc import Sequence\n\n\n"
        "def average_of_numbers(numbers: Sequence[float] | None) -> float:\n"
        '    """计算数字序列的平均值，空输入返回 0.0。"""\n'
        "    if numbers is None or len(numbers) == 0:\n"
        "        return 0.0\n"
        "    return float(sum(numbers) / len(numbers))\n"
    ),
    "test_code": (
        "from solution import average_of_numbers\n\n\n"
        "def test_empty_list() -> None:\n"
        "    assert average_of_numbers([]) == 0.0\n\n\n"
        "def test_none_input() -> None:\n"
        "    assert average_of_numbers(None) == 0.0\n\n\n"
        "def test_normal() -> None:\n"
        "    assert average_of_numbers([1, 2, 3]) == 2.0\n"
    ),
}


def fix_code_prompt(
    solution_code: str, test_code: str, issue_lines: list[str], confirmed: ConfirmedRequirement
) -> tuple[str, str]:
    """定向修复环节的 (system, user)。"""
    system = (
        "你是负责缺陷修复的 Python 工程师，只输出一个 JSON 对象。规则：\n"
        "1. 只针对列出的问题修根因，做最小化修改，禁止顺手重构无关代码；\n"
        "2. 函数名、参数签名与公开行为保持不变，继续满足全部边界与约束；\n"
        "3. 若缺陷源于测试缺失或断言错误，修正 test_code，并补一个能复现该缺陷的回归用例；\n"
        "4. 禁止删除或放宽已有断言来让测试通过；\n"
        "5. 输出完整可运行代码，不要输出 diff 或省略号。\n\n"
        "请参照下面示例，体会“只改必要部分 + 补回归测试”的修复方式。\n"
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
