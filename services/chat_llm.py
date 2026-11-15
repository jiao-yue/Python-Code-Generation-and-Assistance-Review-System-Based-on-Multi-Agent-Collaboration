"""
ChatLLMService：新链路专用的大模型服务。

围绕“需求确认 → 代码生成 → 代码审核 → 定向修复”四个环节，每个环节都调用
prompts/chat_prompts.py 里的 few-shot 示例提示词，让模型照着示例精准产出 JSON，
再由 Pydantic 校验，保证结果可以直接驱动后续节点。

云端模型走 OpenAI 兼容接口；本机 Ollama 走原生 /api/chat，避免 qwen3.5 的思考过程
挤占输出 token。两者共用 JSON 解析、错误回灌和重试逻辑。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from config import settings
from models.chat_schemas import (
    CodeReviewItem,
    CodeReviewReport,
    ConfirmedRequirement,
    RagHit,
)
from models.common import Severity
from prompts import chat_prompts


class ChatLLMService:
    """大模型服务：在线时调用真实 API，失败抛出由上层统一处理。"""

    def __init__(self) -> None:
        """读取当前模型配置；本地 Ollama 不需要 Key，也不创建 OpenAI 客户端。"""
        api_key, base_url, model = settings.get_llm_endpoint()
        self.provider = settings.LLM_PROVIDER
        self.local = self.provider == "ollama"
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        # 本地模型没有 Key 也应在线；只有非本地且没有 Key 才退回 Mock。
        self.offline = bool(settings.FORCE_MOCK or (not api_key and not self.local))
        self.max_tokens = settings.OLLAMA_MAX_TOKENS if self.local else settings.LLM_MAX_TOKENS
        self.timeout = settings.OLLAMA_TIMEOUT if self.local else settings.LLM_TIMEOUT
        self._client = None
        if not self.offline and not self.local:
            try:
                from openai import OpenAI

                self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=self.timeout)
            except Exception as exc:  # 客户端初始化失败时降级为离线
                print(f"[ChatLLM] 客户端初始化失败，转离线：{exc}")
                self.offline = True

    # ------------------------------------------------------------------
    # 最底层：调用一次模型并返回 JSON dict（带重试）
    # ------------------------------------------------------------------
    def _chat_json(self, system: str, user: str) -> dict[str, Any]:
        """调用模型并解析 JSON，失败时把错误回灌给模型自动重试。"""
        last_error = ""
        for _attempt in range(1, settings.LLM_MAX_RETRIES + 1):
            current_user = user
            if last_error:
                current_user += f"\n\n上一次输出不合法（{last_error}），请只输出修正后的 JSON。"
            try:
                content = self._complete(system, current_user, json_mode=True)
                return self._parse_json(content)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        raise RuntimeError(f"ChatLLM 重试耗尽：{last_error}")

    def _chat_text(self, system: str, user: str) -> str:
        """调用模型返回纯文本，复用同一套本地/云端分流与重试逻辑。"""
        last_error = ""
        for _attempt in range(1, settings.LLM_MAX_RETRIES + 1):
            current_user = user
            if last_error:
                current_user += f"\n\n上一次输出不可用（{last_error}），请重新输出完整内容。"
            try:
                content = self._complete(system, current_user, json_mode=False)
                if not content or not content.strip():
                    raise ValueError("模型返回空内容")
                return content
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        raise RuntimeError(f"文本生成重试耗尽：{last_error}")

    def _complete(self, system: str, user: str, json_mode: bool) -> str:
        """按提供商选择调用方式：本地走 Ollama 原生接口，云端走 OpenAI 兼容接口。"""
        if self.local:
            return self._ollama_chat(system, user, json_mode)
        return self._openai_chat(system, user, json_mode)

    def _openai_chat(self, system: str, user: str, json_mode: bool) -> str:
        """调用云端 OpenAI 兼容接口，返回模型正文。"""
        kwargs: dict[str, Any] = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=self.max_tokens,
            **kwargs,
        )
        return response.choices[0].message.content or ""

    def _ollama_chat(self, system: str, user: str, json_mode: bool) -> str:
        """调用本机 Ollama 原生 /api/chat，返回模型正文。"""
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/v1"):  # 兼容误填的 OpenAI 兼容地址
            base_url = base_url[:-3]
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "think": settings.OLLAMA_THINK,
            "keep_alive": settings.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": settings.LLM_TEMPERATURE,
                "num_predict": self.max_tokens,
                "num_ctx": settings.OLLAMA_NUM_CTX,
            },
        }
        if json_mode:
            payload["format"] = "json"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{base_url}/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Ollama HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama 连接失败：{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Ollama 返回的不是合法 JSON 响应") from exc
        content = (body.get("message") or {}).get("content") or ""
        if not content.strip():
            raise ValueError("Ollama 返回空内容，请检查模型名、显存或 OLLAMA_MAX_TOKENS")
        return content

    # ==================================================================
    # 环节 1：需求确认
    # ==================================================================
    def confirm_requirement(self, raw_text: str) -> ConfirmedRequirement:
        """把用户的口语化需求整理成结构化需求。"""
        system, user = chat_prompts.confirm_requirement_prompt(raw_text)
        data = self._chat_json(system, user)
        return ConfirmedRequirement.model_validate(data)

    def refine_requirement(
        self, raw_text: str, confirmed: ConfirmedRequirement, answers: list[dict[str, str]]
    ) -> ConfirmedRequirement:
        """把用户对澄清问题的回答合并进需求契约。"""
        system, user = chat_prompts.refine_requirement_prompt(raw_text, confirmed, answers)
        data = self._chat_json(system, user)
        return ConfirmedRequirement.model_validate(data)

    # ==================================================================
    # 环节 2：代码生成
    # ==================================================================
    def generate_solution(
        self, confirmed: ConfirmedRequirement, knowledge_hits: list[RagHit] | None = None
    ) -> dict[str, str]:
        """同时生成实现代码与 pytest 测试代码（可注入知识库命中的规范）。"""
        system, user = chat_prompts.generate_solution_prompt(confirmed, knowledge_hits)
        last_error = ""
        for _attempt in range(2):
            current_user = user
            if last_error:
                current_user += (
                    f"\n\n上一次输出不可用（{last_error}），"
                    "请只输出包含非空 solution_code 与 test_code 的 JSON。"
                )
            data = self._chat_json(system, current_user)
            solution = self._extract_code(data.get("solution_code", ""))
            test = self._extract_code(data.get("test_code", ""))
            if not solution:
                last_error = "solution_code 为空"
                continue
            if not test:
                last_error = "test_code 为空"
                continue
            if f"def {confirmed.function_name}" not in solution:
                last_error = f"实现中缺少函数 {confirmed.function_name}"
                continue
            return {"solution_code": solution, "test_code": test}
        raise RuntimeError(f"代码生成结果不完整：{last_error}")

    # ==================================================================
    # 环节 3：代码审核
    # ==================================================================
    def review_code(
        self,
        solution_code: str,
        requirement: str,
        test_code: str = "",
        knowledge_hits: list[RagHit] | None = None,
        acceptance_criteria: list[str] | None = None,
    ) -> CodeReviewReport:
        """以独立上下文从规范、安全、逻辑三视角审核代码（可注入知识库命中的规范）。"""
        system, user = chat_prompts.review_code_prompt(
            solution_code, requirement, test_code, knowledge_hits, acceptance_criteria
        )
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
        issue_lines = [self._format_issue(it) for it in items]
        system, user = chat_prompts.fix_code_prompt(solution_code, test_code, issue_lines, confirmed)
        data = self._chat_json(system, user)
        return {
            "solution_code": self._extract_code(data.get("solution_code", solution_code)),
            "test_code": self._extract_code(data.get("test_code", test_code)),
        }

    # ==================================================================
    # 工具方法
    # ==================================================================
    @staticmethod
    def _format_issue(item: CodeReviewItem) -> str:
        """把一条审核问题格式化成修复提示词中的单行描述。"""
        location = f"行{item.line_no}" if item.line_no else "定位见代码"
        return (
            f"{location} [{item.view}/{item.severity.value}] {item.issue_type}："
            f"{item.description} 建议：{item.suggestion}"
        )

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """容错解析模型输出，并保证顶层一定是 JSON 对象。"""

        def require_object(value: Any, source: str) -> dict[str, Any]:
            if not isinstance(value, dict):
                raise json.JSONDecodeError("模型输出的 JSON 顶层必须是对象", source, 0)
            return value

        text = raw.strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            return require_object(value, text)
        try:
            value, _end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            pass
        else:
            return require_object(value, text)
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return require_object(json.loads(text[start : end + 1]), text)
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
        if number > 1.0:
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
