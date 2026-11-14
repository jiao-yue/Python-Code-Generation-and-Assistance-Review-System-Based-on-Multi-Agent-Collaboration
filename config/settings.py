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
# 大模型配置（DeepSeek / 火山方舟，均为 OpenAI 兼容接口）
# ---------------------------------------------------------------------------
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "deepseek").lower()  # deepseek / doubao / mock
LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.0"))  # 固定温度，结果可复现
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "2048"))
LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "60"))
LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "3"))  # JSON 解析失败/超时的重试次数

DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

DOUBAO_API_KEY: str = os.getenv("DOUBAO_API_KEY", "")
DOUBAO_BASE_URL: str = os.getenv("DOUBAO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
DOUBAO_ENDPOINT_ID: str = os.getenv("DOUBAO_ENDPOINT_ID", "")

# 全局开关：置 1 时强制离线，不访问任何外部大模型
FORCE_MOCK: bool = os.getenv("FORCE_MOCK", "0") == "1"


def get_llm_endpoint() -> tuple[str, str, str]:
    """返回当前选用模型的 (api_key, base_url, model)。

    选择了火山方舟且配置了 Key 时走方舟；否则默认走 DeepSeek。
    """
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


# ---------------------------------------------------------------------------
# 流程收敛配置
# ---------------------------------------------------------------------------
MAX_ROUNDS: int = int(os.getenv("MAX_ROUNDS", "3"))  # 最大修复轮次
PYTEST_TIMEOUT: int = int(os.getenv("PYTEST_TIMEOUT", "120"))  # pytest 子进程超时（秒）


# ---------------------------------------------------------------------------
# RAG 检索配置（Chroma 向量库）
# ---------------------------------------------------------------------------
RAG_COLLECTION: str = os.getenv("RAG_COLLECTION", "code_review_kb")
RAG_EMBED_DIM: int = int(os.getenv("RAG_EMBED_DIM", "512"))  # 本地哈希向量维度
RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "4"))  # 审核时最多注入的片段数
RAG_GENERATE_TOP_K: int = int(os.getenv("RAG_GENERATE_TOP_K", "3"))  # 生成时最多注入的片段数
RAG_MIN_SCORE: float = float(os.getenv("RAG_MIN_SCORE", "0.08"))  # 低于该相关度的片段丢弃
RAG_MAX_CHUNK_CHARS: int = int(os.getenv("RAG_MAX_CHUNK_CHARS", "600"))  # 单片注入上限
# 查询扩展：一次检索最多使用几个查询变体（原始查询 + 同义词扩展）
RAG_QUERY_VARIANTS: int = int(os.getenv("RAG_QUERY_VARIANTS", "2"))
# MMR 多样性重排：1.0 只看相关度，越小越强调片段之间的差异
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
# 旧版 .doc 需要外部 Office/LibreOffice，不在跨平台链路中静默伪支持。
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
