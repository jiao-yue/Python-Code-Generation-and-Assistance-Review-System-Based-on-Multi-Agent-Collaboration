"""静态工具运行基类：统一子进程调用与超时处理。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_module(args: list[str], timeout: int = 60, cwd: str | None = None) -> tuple[int, str, str]:
    """
    用当前 Python 解释器以“模块方式”运行外部工具（python -m ruff / bandit / mypy）。

    :param args: 传给工具的参数列表（不含解释器与 -m）
    :param timeout: 超时秒数
    :param cwd: 工作目录
    :return: (returncode, stdout, stderr)
    """
    cmd = [sys.executable, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "工具执行超时"
    except FileNotFoundError as e:
        return -2, "", f"工具不存在：{e}"


def relativize(path: str | Path, root: str | Path) -> str:
    """把工具输出的路径转成相对 root 的 posix 路径，供 Ruff/Bandit 展示统一口径。

    root 是文件（单文件检查）或路径不在 root 下时，回退为文件名。
    """
    file_path = Path(path)
    root_path = Path(root)
    if not root_path.is_dir():
        return file_path.name
    try:
        return file_path.resolve().relative_to(root_path.resolve()).as_posix()
    except ValueError:
        return file_path.name
