"""目标语言识别与项目形态判定。

不依赖大模型：先用确定性关键词判断语言，能让离线兜底和真实 API 走同一条路径；
大模型返回的 language 只作为补充，最终由这里归一化，避免出现 "C++"、"前端" 等
无法落盘的口径。
"""

from __future__ import annotations

import re

LANGUAGE_LABELS: dict[str, str] = {
    "python": "Python",
    "java": "Java",
    "c": "C",
    "html": "HTML / CSS / JavaScript",
}

_PROJECT_HINTS = (
    # 这些词是“用户想要一个完整工程”的强信号。
    # 例如“做一个待办项目”“写个网页”“带文件结构的管理系统”，
    # 只要命中一个，就把 Python 从单函数模式切换到多文件项目模式。
    "项目",
    "系统",
    "应用",
    "网站",
    "网页",
    "页面",
    "前端",
    "后端",
    "游戏",
    "工具",
    "平台",
    "小程序",
    "管理系统",
    "多文件",
    "文件结构",
    "目录结构",
)


def normalize_language(value: str | None, raw_text: str = "") -> str:
    """把模型/用户给出的语言名归一化为 python/java/c/html。"""
    # 把“模型说的语言”和“用户原话”拼在一起判断，避免模型只返回空值。
    text = f"{value or ''} {raw_text}".lower()
    # HTML 放最前面：网页项目常常同时出现 CSS/JavaScript/Python 后端等词，
    # 但用户真正想要的是可直接打开的前端页面，所以前端关键词优先级最高。
    if any(key in text for key in ("html", "css", "javascript", "前端", "网页", "网站", "页面")):
        return "html"
    if any(key in text for key in ("java", "spring", "安卓", "android")):
        return "java"
    # (?<![a-z]) 表示“前面不能紧跟英文字母”，避免把 "abc" 里的 c 误判成 C 语言；
    # \.c\b 则专门匹配文件名结尾的 .c。
    if re.search(r"(?<![a-z])(c语言|c 语言|gcc|\.c\b)", text):
        return "c"
    if any(key in text for key in ("python", "pytest", "django", "flask")):
        return "python"
    # 什么语言都没说时，沿用系统原来的默认值 Python，保证旧功能行为不变。
    return "python"


def detect_language(raw_text: str) -> str:
    """从用户需求中识别目标语言；没有明确语言时按 Python 处理。"""
    return normalize_language("", raw_text)


def detect_file_hints(raw_text: str) -> list[str]:
    """提取需求中点名的文件，如 index.html、Main.java、src/app.py。

    正则里的长扩展名放在短扩展名前（python 在 py 前、html 在 htm 前），
    这样 "index.html" 不会被截成 "index.h"，"main.python" 也不会只抓到 "py"。
    """
    pattern = r"[A-Za-z0-9_./-]+\.(?:python|py|java|html|htm|css|js|json|md|txt|c|h)\b"
    # set 去重，sorted 让结果顺序稳定，方便测试和前端展示。
    return sorted(set(re.findall(pattern, raw_text, flags=re.IGNORECASE)))


def detect_project_type(raw_text: str, language: str, file_hints: list[str]) -> str:
    """判断交付形态：function 还是 project。

    非 Python 语言一律按 project 处理：Java 需要类文件，C 需要源文件/头文件，
    HTML 天然包含 HTML/CSS/JS 多文件。Python 只有在明确提到项目/系统/多文件
    时才进入 project，保留原单函数生成与 HumanEval 评测链路。
    """
    if language != "python":
        # Java/C 不可能只靠一个“函数”交付：Java 至少要有类文件，
        # C 至少要有 .c/.h，所以直接按项目处理。
        return "project"
    if file_hints:
        # 用户点名了 index.html、src/main.py 这类文件，说明他要的是文件集合。
        return "project"
    lowered = raw_text.lower()
    if any(hint in lowered for hint in _PROJECT_HINTS):
        return "project"
    # 其余情况（例如“写一个二分查找函数”“实现两数之和”）保持原来的单函数链路，
    # 这样 HumanEval 等旧评测不会被误改成多文件项目。
    return "function"


def safe_project_name(title: str, language: str) -> str:
    """从标题生成可读的工程名，保留中文，去掉路径分隔符。

    工程名只用于展示和压缩包命名，不参与磁盘路径拼接；
    这里仍然清掉 ``\\ / : * ? " < > |``，避免下载时被浏览器当成非法文件名。
    ``[:40]`` 限制长度，防止标题太长导致部分系统路径超限。
    """
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "_", (title or "").strip()).strip("_")
    cleaned = cleaned[:40] or f"{language}_project"
    return cleaned
