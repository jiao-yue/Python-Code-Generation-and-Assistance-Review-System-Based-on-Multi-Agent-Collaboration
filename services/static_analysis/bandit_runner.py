"""
Bandit 运行器（对应“安全视角：Bandit 查安全隐患”）。

输出 JSON 由 Bandit 官方保证可解析，映射为 ToolFinding。
"""

from __future__ import annotations

import json
from pathlib import Path

from models.common import ToolFinding
from services.static_analysis.base_runner import run_module
from services.static_analysis.severity_mapper import map_bandit


def run_bandit(target: str | Path, timeout: int = 60) -> list[ToolFinding]:
    """
    对目标目录运行 Bandit 安全扫描。

    :param target: 工程目录
    :return: ToolFinding 列表
    """
    target_path = Path(target)
    target = str(target_path)

    # 收集测试文件（test_*.py / *_test.py），安全扫描只针对生产代码，避免 pytest 的 assert 误报
    test_files = [
        str(p)
        for p in target_path.rglob("*.py")
        if p.name.startswith("test_") or p.name.endswith("_test.py")
    ]
    args = ["-m", "bandit", "-r", target, "-f", "json", "-q"]
    if test_files:
        args += ["-x", ",".join(test_files)]
    # -f json：结构化输出；-q：安静模式；-x：排除测试文件
    rc, out, err = run_module(args, timeout=timeout)
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        print(f"[Bandit] 输出无法解析：{err or out[:200]}")
        return []

    findings: list[ToolFinding] = []
    for rec in data.get("results", []):
        test_id = rec.get("test_id", "")
        bandit_sev = rec.get("issue_severity", "")
        findings.append(
            ToolFinding(
                source="tool",
                tool="Bandit",
                rule_code=test_id,
                severity=map_bandit(test_id, bandit_sev),
                file_path=Path(rec.get("filename", "solution.py")).name,
                line_no=rec.get("line_number"),
                message=f"{rec.get('test_name', '')}: {rec.get('issue_text', '')}",
            )
        )
    return findings
