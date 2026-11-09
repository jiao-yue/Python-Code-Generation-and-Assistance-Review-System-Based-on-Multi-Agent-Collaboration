"""
严重度映射（对应“工具规则码映射为统一 severity 后与 LLM 发现合并去重”）。

把 Ruff / Bandit / ast / MyPy 各自的规则码与严重度，统一映射为三级：
    阻断（不可运行/安全隐患）、主要（逻辑/边界/异常）、次要（规范/命名）。
"""

from __future__ import annotations

from models.common import Severity

# ---------------------------------------------------------------------------
# 阻断级规则（不可运行 / 明确安全隐患）
# ---------------------------------------------------------------------------
_BLOCKER_RUFF: set[str] = {
    "E999",  # 语法错误，无法运行
    "E902",  # IO 错误
}
_BLOCKER_BANDIT: set[str] = {
    "B307",  # eval/exec 调用
    "B602",  # subprocess shell=True
    "B605",  # os.system 启动 shell 进程
    "B604",  # start_process_with_shell（shell=True 变体）
    "B603",  # 无 shell 的进程调用（可执行注入，酌情）
    "B301",  # pickle 加载
    "B302",  # marshal 加载
    "B303",  # md5 弱哈希
    "B324",  # 不安全的 hashlib 用法
    "B321",  # FTP 明文传输
    "B501",  # requests verify=False
    "B502",  # SSL 版本不安全
    "B105",  # 硬编码密码字符串
    "B106",  # 硬编码密码
    "B107",  # 硬编码默认密码
}

# ---------------------------------------------------------------------------
# 主要级规则（潜在错误 / 逻辑 / 边界）
# ---------------------------------------------------------------------------
_MAJOR_RUFF: set[str] = {
    "F821",  # 未定义名称
    "F811",  # 名称重复定义
    "F822",  # 导出名称未定义
    "F823",  # 局部变量引用前赋值
    "F831",  # 元组解包重复
    "F842",  # 循环变量注解未使用
    "E752",  # 隐式字符串拼接
    "E722",  # 裸 except
    "B001",  # 裸 f-string 元组
    "B012",  # break/continue 跳出 finally
    "B018",  # 无意义的表达式
    "B902",  # 无 self 的方法
    "B023",  # 函数定义未绑定循环闭包
}
_MAJOR_BANDIT: set[str] = {
    # B101（assert）为 Bandit Low，且测试文件已排除，不再列为主要
    "B110",  # try/except/pass
    "B112",  # try/except/continue
    "B001",  # 注意：B001 在 ruff 中存在，bandit 无此码
    "B311",  # 伪随机用于安全场景
    "B323",  # 不可信的临时目录
    "B102",  # 以 _ 开头的 set_password
    "B103",  # set_bad_password
    "B104",  # hardcoded_bind_all
    "B108",  # hardcoded_tmp
}


def map_ruff(code: str) -> Severity:
    """Ruff 规则码 → 统一 severity。"""
    if not code:
        return Severity.MINOR
    if code in _BLOCKER_RUFF:
        return Severity.BLOCKER
    if code in _MAJOR_RUFF:
        return Severity.MAJOR
    # F 开头（pyflakes，潜在错误）默认主要，其余 E/W 规范类默认次要
    if code.startswith("F") and code not in {"F401", "F841", "F403", "F405"}:
        return Severity.MAJOR
    return Severity.MINOR


def map_bandit(test_id: str, bandit_severity: str = "") -> Severity:
    """
    Bandit 规则码 → 统一 severity。

    :param test_id: 如 B307
    :param bandit_severity: Bandit 自带 HIGH/MEDIUM/LOW
    """
    if test_id in _BLOCKER_BANDIT:
        return Severity.BLOCKER
    if test_id in _MAJOR_BANDIT:
        return Severity.MAJOR
    # 回退到 Bandit 自带严重度
    if bandit_severity:
        sev = bandit_severity.upper()
        if sev == "HIGH":
            return Severity.BLOCKER
        if sev == "MEDIUM":
            return Severity.MAJOR
    return Severity.MINOR


def map_ast(message: str) -> Severity:
    """ast 检查问题 → severity（语法不可运行属阻断，其余结构提示属次要）。"""
    if "语法" in message or "Syntax" in message or "无法" in message:
        return Severity.BLOCKER
    return Severity.MINOR
