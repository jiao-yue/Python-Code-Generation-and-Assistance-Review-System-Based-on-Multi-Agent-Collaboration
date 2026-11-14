"""
全局配置模块。

集中管理两类配置：
1. 路径与目录：运行期产物统一放在 storage/ 下，启动时自动创建；
2. 大模型与数据库：敏感信息（API Key、数据库口令）从项目根目录的 .env 读取，
   不在代码里硬编码。

当未配置大模型 Key 或显式开启 FORCE_MOCK 时，系统会自动进入离线模式，
用确定性兜底逻辑跑通整条链路，保证在没有网络时也能演示与测试。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 路径配置
# ---------------------------------------------------------------------------
# 项目根目录（本文件位于 <root>/config/settings.py，向上一级即根目录）
BASE_DIR: Path = Path(__file__).resolve().parent.parent

# 运行期产物目录（生成的临时工程、日志、结果等），均不提交到 Git
STORAGE_DIR: Path = BASE_DIR / "storage"
GENERATED_DIR: Path = STORAGE_DIR / "generated"  # 每次运行的独立 Python 工程
LOGS_DIR: Path = STORAGE_DIR / "logs"
RESULTS_DIR: Path = STORAGE_DIR / "results"

# 审核知识库（随代码提交）与向量索引（运行期产物，自动重建）
KNOWLEDGE_DIR: Path = BASE_DIR / "knowledge"
RAG_DIR: Path = STORAGE_DIR / "rag"
# 用户上传的原件、提取文本和 MD5 清单（运行期数据，不提交到 Git）
UPLOAD_DIR: Path = STORAGE_DIR / "uploads"
UPLOAD_MANIFEST_PATH: Path = UPLOAD_DIR / "manifest.json"

# 自动创建运行期目录，避免后续写文件时因目录不存在而报错
for _directory in (STORAGE_DIR, GENERATED_DIR, LOGS_DIR, RESULTS_DIR, RAG_DIR, UPLOAD_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

# 加载根目录 .env（包含 DeepSeek / 火山方舟的 API Key）
load_dotenv(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# 大模型配置（DeepSeek / 火山方舟 / Ollama 本地模型 / 内置 Mock）
# ---------------------------------------------------------------------------
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "deepseek").lower()
# 可选值：deepseek / doubao / ollama / mock
LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.0"))  # 固定温度，结果可复现
# 默认 8192：代码/网页类长输出在 2048 时容易被截断成不完整 JSON。
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "8192"))
LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "60"))
LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "3"))  # JSON 解析失败/超时的重试次数

DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

DOUBAO_API_KEY: str = os.getenv("DOUBAO_API_KEY", "")
DOUBAO_BASE_URL: str = os.getenv("DOUBAO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
DOUBAO_ENDPOINT_ID: str = os.getenv("DOUBAO_ENDPOINT_ID", "")

# 本地 Ollama：走原生 /api/chat 接口，不需要 API Key。
# 地址填服务根地址（不带 /v1）；模型名必须与 `ollama list` 中的名称一致。
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
OLLAMA_API_KEY: str = os.getenv("OLLAMA_API_KEY", "")  # 仅远程/带鉴权的 Ollama 需要
OLLAMA_TIMEOUT: int = int(os.getenv("OLLAMA_TIMEOUT", "600"))  # 本地生成较慢，单独给足超时
OLLAMA_MAX_TOKENS: int = int(os.getenv("OLLAMA_MAX_TOKENS", "8192"))
OLLAMA_NUM_CTX: int = int(os.getenv("OLLAMA_NUM_CTX", "16384"))  # 上下文窗口
# qwen3.5 默认会先输出思考过程，耗时长；默认关闭，需要推理时设为 1。
OLLAMA_THINK: bool = os.getenv("OLLAMA_THINK", "0") == "1"
OLLAMA_KEEP_ALIVE: str = os.getenv("OLLAMA_KEEP_ALIVE", "30m")  # 生成完继续驻留显存/内存

# 全局开关：置 1 时强制离线，不访问任何外部大模型
FORCE_MOCK: bool = os.getenv("FORCE_MOCK", "0") == "1"


def get_llm_endpoint() -> tuple[str, str, str]:
    """返回当前选用模型的 (api_key, base_url, model)。

    - ``ollama``：本机原生接口，不需要 Key；
    - ``doubao``：配置了方舟 Key 时走方舟；
    - ``mock``：显式离线，返回空 Key，交给内置兜底逻辑；
    - 其余情况默认走 DeepSeek。
    """
    if LLM_PROVIDER == "ollama":
        # Key 只用于可选鉴权；为空也保持“在线”，由本地服务提供模型。
        return OLLAMA_API_KEY, OLLAMA_BASE_URL, OLLAMA_MODEL
    if LLM_PROVIDER == "mock":
        return "", "", LLM_MODEL
    if LLM_PROVIDER == "doubao" and DOUBAO_API_KEY:
        model = DOUBAO_ENDPOINT_ID or "deepseek-v4-pro-ga-260813"
        return DOUBAO_API_KEY, DOUBAO_BASE_URL, model
    return DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, LLM_MODEL


# ---------------------------------------------------------------------------
# MySQL 数据库配置（用于持久化保存会话与每轮结果）
# ---------------------------------------------------------------------------
DB_HOST: str = os.getenv("DB_HOST", "localhost")
DB_PORT: int = int(os.getenv("DB_PORT", "3306"))
DB_USER: str = os.getenv("DB_USER", "root")
DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
# 库名含空格，SQL 中必须使用反引号：`graduation project`
DB_NAME: str = os.getenv("DB_NAME", "graduation project")
DB_CHARSET: str = "utf8mb4"

# 测试模式：开启后，服务自动生成的会话编号带 test_ 前缀。
# 测试结束时只清理这个前缀的会话，普通用户会话不受影响。
TEST_MODE: bool = os.getenv("TEST_MODE", "0") == "1"


# ---------------------------------------------------------------------------
# 流程收敛配置
# ---------------------------------------------------------------------------
MAX_ROUNDS: int = int(os.getenv("MAX_ROUNDS", "3"))  # 最大修复轮次
PYTEST_TIMEOUT: int = int(os.getenv("PYTEST_TIMEOUT", "120"))  # pytest 子进程超时（秒）

REVIEW_PARALLEL: bool = os.getenv("REVIEW_PARALLEL", "1") == "1"
REVIEW_MAX_WORKERS: int = int(os.getenv("REVIEW_MAX_WORKERS", "4"))
EXPORT_MAX_BYTES: int = int(os.getenv("EXPORT_MAX_BYTES", str(20 * 1024 * 1024)))


# ---------------------------------------------------------------------------
# 评测与消融实验配置
# ---------------------------------------------------------------------------
EVAL_DIR: Path = BASE_DIR / "evaluation"
EVAL_DATASET_DIR: Path = EVAL_DIR / "datasets"
EVAL_RESULT_DIR: Path = RESULTS_DIR
EVAL_DEFAULT_LIMIT: int = int(os.getenv("EVAL_DEFAULT_LIMIT", "40"))
EVAL_MAX_ROUNDS: int = int(os.getenv("EVAL_MAX_ROUNDS", str(MAX_ROUNDS)))
EVAL_PYTEST_TIMEOUT: int = int(os.getenv("EVAL_PYTEST_TIMEOUT", str(PYTEST_TIMEOUT)))


# ---------------------------------------------------------------------------
# RAG 检索配置（Chroma 向量库）
# ---------------------------------------------------------------------------
RAG_COLLECTION: str = os.getenv("RAG_COLLECTION", "code_review_kb")
RAG_EMBED_DIM: int = int(os.getenv("RAG_EMBED_DIM", "512"))
RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "4"))
RAG_GENERATE_TOP_K: int = int(os.getenv("RAG_GENERATE_TOP_K", "3"))
RAG_MIN_SCORE: float = float(os.getenv("RAG_MIN_SCORE", "0.08"))
RAG_MAX_CHUNK_CHARS: int = int(os.getenv("RAG_MAX_CHUNK_CHARS", "600"))
RAG_QUERY_VARIANTS: int = int(os.getenv("RAG_QUERY_VARIANTS", "2"))
RAG_MMR_LAMBDA: float = float(os.getenv("RAG_MMR_LAMBDA", "0.85"))


# ---------------------------------------------------------------------------
# 附件上传与文档解析（Word / Excel / PDF / 图片 OCR）
# ---------------------------------------------------------------------------
UPLOAD_MAX_FILES: int = int(os.getenv("UPLOAD_MAX_FILES", "5"))
UPLOAD_MAX_FILE_BYTES: int = int(os.getenv("UPLOAD_MAX_FILE_BYTES", str(20 * 1024 * 1024)))
UPLOAD_MAX_TEXT_CHARS: int = int(os.getenv("UPLOAD_MAX_TEXT_CHARS", "200000"))
UPLOAD_CHAT_FILE_CHARS: int = int(os.getenv("UPLOAD_CHAT_FILE_CHARS", "12000"))
UPLOAD_CHAT_TOTAL_CHARS: int = int(os.getenv("UPLOAD_CHAT_TOTAL_CHARS", "30000"))
UPLOAD_PDF_MAX_PAGES: int = int(os.getenv("UPLOAD_PDF_MAX_PAGES", "100"))
UPLOAD_EXCEL_MAX_ROWS: int = int(os.getenv("UPLOAD_EXCEL_MAX_ROWS", "10000"))
UPLOAD_MAX_IMAGE_PIXELS: int = int(os.getenv("UPLOAD_MAX_IMAGE_PIXELS", "40000000"))
UPLOAD_MAX_UNCOMPRESSED_BYTES: int = int(
    os.getenv("UPLOAD_MAX_UNCOMPRESSED_BYTES", str(100 * 1024 * 1024))
)
UPLOAD_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".docx",
        ".xlsx",
        ".xls",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".bmp",
        ".webp",
        ".tif",
        ".tiff",
        ".txt",
        ".py",
        ".md",
        ".csv",
        ".json",
        ".log",
    }
)
