"""
ChatLLMService：新链路专用的大模型服务。

围绕“需求确认 → 代码生成 → 代码审核 → 定向修复”四个环节，每个环节都调用
prompts/chat_prompts.py 里的 few-shot 示例提示词，让模型照着示例精准产出 JSON，
再由 Pydantic 校验，保证结果可以直接驱动后续节点。

阅读顺序：__init__ → _chat_json → confirm_requirement →
        generate_solution → review_code → fix_code → 工具方法。
"""

from __future__ import annotations

import json
import re
from typing import Any

from config import settings
from models.chat_schemas import (
    CodeReviewItem,
    CodeReviewReport,
    ConfirmedRequirement,
)
from models.common import Severity
from prompts import chat_prompts


class ChatLLMService:
    """大模型服务：在线时调用真实 API，失败抛出由上层统一处理。"""

    def __init__(self) -> None:
        """读取当前模型配置并创建 OpenAI 兼容客户端。"""
        api_key, base_url, model = settings.get_llm_endpoint()
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        # FORCE_MOCK 或没有 Key 时标记为离线，节点会改用确定性兜底逻辑
        self.offline = bool(settings.FORCE_MOCK or not api_key)
        self._client = None
        if not self.offline:
            try:
                from openai import OpenAI

                self._client = OpenAI(
                    api_key=api_key, base_url=base_url, timeout=settings.LLM_TIMEOUT
                )
            except Exception as exc:  # 客户端初始化失败时降级为离线
                print(f"[ChatLLM] 客户端初始化失败，转离线：{exc}")
                self.offline = True

    # ------------------------------------------------------------------
    # 最底层：调用一次模型并返回 JSON dict（带重试）
    # ------------------------------------------------------------------
    def _chat_json(self, system: str, user: str) -> dict[str, Any]:
        """调用模型并解析 JSON，失败时把错误回灌给模型自动重试。

        :raises RuntimeError: 重试次数耗尽仍无法得到合法 JSON
        """
        last_error = ""
        for _attempt in range(1, settings.LLM_MAX_RETRIES + 1):
            current_user = user
            if last_error:
                current_user += f"\n\n上一次输出不合法（{last_error}），请只输出修正后的 JSON。"
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": current_user},
                    ],
                    temperature=settings.LLM_TEMPERATURE,
                    max_tokens=settings.LLM_MAX_TOKENS,
                    response_format={"type": "json_object"},  # 强制输出 JSON
                )
                content = response.choices[0].message.content
                return self._parse_json(content)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        raise RuntimeError(f"ChatLLM 重试耗尽：{last_error}")

    # ==================================================================
    # 环节 1：需求确认
    # ==================================================================
    def confirm_requirement(self, raw_text: str) -> ConfirmedRequirement:
        """把用户的口语化需求整理成结构化需求。"""
        system, user = chat_prompts.confirm_requirement_prompt(raw_text)
        data = self._chat_json(system, user)
        return ConfirmedRequirement.model_validate(data)

    # ==================================================================
    # 环节 2：代码生成
    # ==================================================================
    def generate_solution(self, confirmed: ConfirmedRequirement) -> dict[str, str]:
        """同时生成实现代码与 pytest 测试代码。"""
        system, user = chat_prompts.generate_solution_prompt(confirmed)
        data = self._chat_json(system, user)
        return {
            "solution_code": self._extract_code(data.get("solution_code", "")),
            "test_code": self._extract_code(data.get("test_code", "")),
        }

    # ==================================================================
    # 环节 3：代码审核
    # ==================================================================
    def review_code(
        self, solution_code: str, requirement: str, test_code: str = ""
    ) -> CodeReviewReport:
        """以独立上下文从规范、安全、逻辑三视角审核代码。"""
        system, user = chat_prompts.review_code_prompt(solution_code, requirement, test_code)
        data = self._chat_json(system, user)

        items = [
            CodeReviewItem(
                view=str(raw.get("view", "logic")),
                severity=self.to_severity(raw.get("severity")),
                line_no=raw.get("line_no"),
                issue_type=str(raw.get("issue_type", "代码问题")),
                description=str(raw.get("description", "")),
                suggestion=str(raw.get("suggestion", "")),
                source="llm",
                confidence=self._to_confidence(raw.get("confidence")),
            )
            for raw in (data.get("items") or [])
        ]
        return self.build_report(items, str(data.get("summary", "")))

    # ==================================================================
    # 环节 4：定向修复
    # ==================================================================
    def fix_code(
        self,
        solution_code: str,
        test_code: str,
        items: list[CodeReviewItem],
        confirmed: ConfirmedRequirement,
    ) -> dict[str, str]:
        """按审核问题做最小化修复。"""
        issue_lines = [
            f"行{it.line_no} [{it.view}/{it.severity.value}] {it.issue_type}："
            f"{it.description} 建议：{it.suggestion}"
            for it in items
        ]
        system, user = chat_prompts.fix_code_prompt(
            solution_code, test_code, issue_lines, confirmed
        )
        data = self._chat_json(system, user)
        return {
            "solution_code": self._extract_code(data.get("solution_code", solution_code)),
            "test_code": self._extract_code(data.get("test_code", test_code)),
        }

    # ==================================================================
    # 工具方法
    # ==================================================================
    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """容错解析模型输出，返回第一个完整 JSON 对象。

        依次尝试：直接解析 → raw_decode 取首个 JSON → 截取最外层花括号。
        """
        text = raw.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        try:
            value, _end = json.JSONDecoder().raw_decode(text)
            return value
        except json.JSONDecodeError:
            pass
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise json.JSONDecodeError("未找到可解析的 JSON 对象", text, 0)

    @staticmethod
    def _extract_code(text: Any) -> str:
        """去掉可能存在的 ```python 围栏，返回纯代码。"""
        if not isinstance(text, str):
            return str(text)
        fence = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
        return fence.group(1).strip() if fence else text.strip()

    @staticmethod
    def _to_confidence(value: Any) -> float:
        """把置信度归一化到 0~1，兼容 0.85 与 85 两种写法，无法解析默认 0.8。"""
        if value is None:
            return 0.8
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.8
        if number > 1.0:  # 大于 1 视为百分数
            number /= 100.0
        return max(0.0, min(1.0, number))

    @staticmethod
    def to_severity(value: Any) -> Severity:
        """把英文/中文严重等级映射为统一的三级枚举。"""
        if not value:
            return Severity.MAJOR
        text = str(value).lower()
        if "critical" in text or "block" in text or "阻断" in text:
            return Severity.BLOCKER
        if "medium" in text or "low" in text or "minor" in text or "次要" in text:
            return Severity.MINOR
        return Severity.MAJOR

    @staticmethod
    def build_report(items: list[CodeReviewItem], summary: str) -> CodeReviewReport:
        """统计各等级数量并判定是否通过（无阻断、无主要问题才算通过）。"""
        counts = {"阻断": 0, "主要": 0, "次要": 0}
        for item in items:
            counts[item.severity.value] += 1
        passed = counts["阻断"] == 0 and counts["主要"] == 0
        if not summary:
            summary = (
                "审核通过，无阻断/主要问题。"
                if passed
                else f"发现 {counts['阻断']} 个阻断、{counts['主要']} 个主要问题，需修复。"
            )
        return CodeReviewReport(passed=passed, items=items, summary=summary, counts=counts)
