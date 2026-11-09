"""
TestRunner：可执行测试（对应“支撑服务层 TestRunner：子进程 pytest，超时回收”）。

设计：
1. 在子进程中运行 pytest，与主流程隔离，超时自动回收；
2. 使用 --junitxml 输出结构化结果，精确统计 用例总数/通过/失败；
3. 同时保留 --tb=short 文本，traceback 落日志，便于报告展示。
"""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from config import settings
from models.common import PytestResult


class TestRunner:
    """pytest 子进程执行器。"""

    def __init__(self, timeout: int | None = None) -> None:
        self.timeout = timeout or settings.PYTEST_TIMEOUT

    def run(self, project_dir: str | Path) -> PytestResult:
        """
        在指定工程目录运行 pytest。

        :param project_dir: 生成工程目录（含 solution.py 与 test_*.py）
        :return: PytestResult
        """
        project_dir = Path(project_dir)
        junit_path = project_dir / "junit_report.xml"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--tb=short",
            f"--junitxml={junit_path}",
            str(project_dir),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(project_dir),
                encoding="utf-8",
                errors="replace",
            )
            returncode = proc.returncode
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            # 超时：杀掉进程，返回失败结果
            return PytestResult(
                returncode=-1,
                total=0,
                passed=0,
                failed=0,
                duration=float(self.timeout),
                traceback=f"pytest 执行超时（{self.timeout}s）：{e}",
                all_green=False,
            )

        # 解析 JUnit XML 得到精确计数
        total = passed = failed = 0
        duration = 0.0
        if junit_path.exists():
            try:
                root = ET.parse(junit_path).getroot()
                # 兼容 testsuites / testsuite 两种根节点
                suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
                for suite in suites:
                    total += int(suite.get("tests", 0))
                    failed += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
                    duration += float(suite.get("time", 0))
                passed = total - failed
            except Exception as e:
                print(f"[TestRunner] JUnit XML 解析失败：{e}")

        # 提取 traceback（失败用例详情）
        traceback = self._extract_traceback(stdout)
        return PytestResult(
            returncode=returncode,
            total=total,
            passed=passed,
            failed=failed,
            duration=round(duration, 3),
            traceback=traceback or (stderr or ""),
            all_green=(returncode == 0 and failed == 0 and total > 0),
        )

    @staticmethod
    def _extract_traceback(stdout: str) -> str:
        """从 pytest 输出中截取失败段落（从第一个 FAIL/ERROR 开始）。"""
        markers = ["= FAILURES =", "= ERRORS =", " short test summary info"]
        idx = -1
        for m in markers:
            i = stdout.find(m)
            if i != -1 and (idx == -1 or i < idx):
                idx = i
        return stdout[idx:].strip() if idx != -1 else ""
