"""
Ruff 运行器（对应“规范视角：Ruff 查编码规范与潜在错误”）。

输出 JSON 由 Ruff 官方保证可解析，映射为 ToolFinding。
"""

from __future__ import annotations

import json
from pathlib import Path

from models.common import ToolFinding
from services.static_analysis.base_runner import run_module
from services.static_analysis.severity_mapper import map_ruff


def run_ruff(target: str | Path, timeout: int = 60) -> list[ToolFinding]:
    """
    对目标目录/文件运行 Ruff 检查。

    :param target: 工程目录或文件
    :return: ToolFinding 列表
    """
    target = str(target)
    # --output-format json：结构化输出；--no-cache：避免缓存影响结果
    rc, out, err = run_module(
        ["-m", "ruff", "check", "--output-format", "json", "--no-cache", target],
        timeout=timeout,
    )
    # Ruff 正常情况下即便发现问题也返回 1；rc=2 为工具自身错误
    if not out.strip():
        return []
    try:
        records = json.loads(out)
    except json.JSONDecodeError:
        print(f"[Ruff] 输出无法解析：{err or out[:200]}")
        return []

    findings: list[ToolFinding] = []
    for rec in records:
        code = rec.get("code") or ""
        loc = rec.get("location", {})
        findings.append(
            ToolFinding(
                source="tool",
                tool="Ruff",
                rule_code=code,
                severity=map_ruff(code),
                file_path=Path(rec.get("filename", "solution.py")).name,
                line_no=loc.get("row"),
                message=rec.get("message", ""),
            )
        )
    return findings
