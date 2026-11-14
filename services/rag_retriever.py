"""
RAG 检索服务：把项目规范知识库切分、向量化，并在审核/生成前检索出相关条目。

整体链路（检索增强生成，Retrieval-Augmented Generation）：

    knowledge/*.md
        │  按二级标题切分成片段
        ▼
    本地哈希向量（字符 n-gram + 标识符词元，无需下载模型、不访问网络）
        │  写入 Chroma 持久化集合 storage/rag
        ▼
    查询构造（需求 + 代码 + 从代码里识别出的风险信号）
        │  规则化查询扩展（中英术语同义）
        ▼
    混合检索：向量余弦 + BM25 关键词 + 查询词元覆盖率
        │  MMR 去重后取 Top-K
        ▼
    注入审核/生成提示词，并把命中来源回传给前端

设计取舍：
1. 嵌入用确定性哈希向量而不是在线 embedding 接口：本项目默认走 DeepSeek，
   它不提供 embedding 端点；哈希向量可离线运行、结果可复现，且对"规范条文 +
   代码关键字"这类短文本检索足够有效，不引入模型下载与网络依赖。
2. 索引按知识库内容指纹自动重建：改了 knowledge/*.md 后无需手动刷新。
3. 任何环节失败都降级为空命中，不影响主链路继续跑。
4. 混合检索与查询扩展都是确定性规则：不调用大模型重写查询，避免额外延迟、
   网络依赖和同一代码两次检索结果不一致；评估脚本可复现对比三种检索模式。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from config import settings
from models.chat_schemas import RagHit

# 索引版本：切分规则或向量算法变化时递增，触发全量重建
_INDEX_VERSION = 3  # v3：内置知识 + 上传知识，指纹包含标题与来源信息

# 匹配 ASCII 标识符（含下划线）与中文字符
_ASCII_TOKEN = re.compile(r"[a-z0-9_]{2,32}")
_NON_CJK = re.compile(r"[^\u4e00-\u9fff]+")


# ===========================================================================
# 一、知识库切分
# ===========================================================================
@dataclass(frozen=True)
class _Chunk:
    """知识库中的一个可检索片段（对应 markdown 的一个二级标题小节）。"""

    chunk_id: str
    doc: str
    doc_title: str
    section: str
    content: str


def _split_markdown(path: Path) -> list[_Chunk]:
    """按二级标题 `## ` 把一个 markdown 文档切成若干片段。

    一级标题作为文档标题，只出现在文档开头，不单独成段；
    第二个二级标题之前的引言归入“概述”小节。
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    doc_title = ""
    for line in lines:
        if line.startswith("# "):
            doc_title = line[2:].strip()
            break

    # 先收集每个小节的标题与正文，再统一生成片段
    sections: list[tuple[str, list[str]]] = []
    current_title = "概述"
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            sections.append((current_title, current_lines))
            current_title = line[3:].strip()
            current_lines = []
        elif line.startswith("# "):
            continue  # 一级标题已经作为文档标题，跳过
        else:
            current_lines.append(line)
    sections.append((current_title, current_lines))

    chunks: list[_Chunk] = []
    for title, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body:
            continue  # 只有标题没有正文的小节不建索引
        index = len(chunks) + 1
        chunks.append(
            _Chunk(
                chunk_id=f"{path.stem}#{index}",
                doc=path.stem,
                doc_title=doc_title,
                section=title,
                content=body,
            )
        )
    return chunks


def _load_chunks() -> list[_Chunk]:
    """读取内置知识和已导入的上传附件；仅聊天附件不会进入索引。"""
    from services.upload_service import upload_service

    paths: list[Path] = []
    if settings.KNOWLEDGE_DIR.exists():
        paths.extend(sorted(settings.KNOWLEDGE_DIR.glob("*.md")))
    paths.extend(upload_service.knowledge_files())

    chunks: list[_Chunk] = []
    for path in paths:
        chunks.extend(_split_markdown(path))
    return chunks


def _fingerprint(chunks: list[_Chunk]) -> str:
    """知识库内容指纹：内容或算法版本变化时索引需要重建。"""
    digest = hashlib.sha256()
    digest.update(f"v{_INDEX_VERSION}|dim{settings.RAG_EMBED_DIM}".encode())
    for chunk in sorted(chunks, key=lambda item: item.chunk_id):
        digest.update(chunk.chunk_id.encode("utf-8"))
        digest.update(chunk.doc_title.encode("utf-8"))
        digest.update(chunk.section.encode("utf-8"))
        digest.update(chunk.content.encode("utf-8"))
    return digest.hexdigest()


# ===========================================================================
# 二、本地确定性向量
# ===========================================================================
def _features(text: str) -> list[str]:
    """把文本拆成用于哈希的特征：ASCII 词元 + 中文 2/3 字符 n-gram。

    中文按字符滑窗切分，无需分词器；ASCII 同时保留整体标识符和下划线分段，
    这样 `pickle.loads` 既能匹配 "pickle"，也能匹配 "loads"。
    """
    lowered = text.lower()
    features: list[str] = []
    for token in _ASCII_TOKEN.findall(lowered):
        features.append("w:" + token)
        features.extend("w:" + part for part in token.split("_") if len(part) >= 3)

    cjk = _NON_CJK.sub("", lowered)
    for size in (2, 3):
        features.extend(
            f"c{size}:" + cjk[start : start + size] for start in range(len(cjk) - size + 1)
        )
    return features


def _embed_text(text: str, dim: int) -> list[float]:
    """把一段文本映射成固定维度的稀疏哈希向量，并做 L2 归一化。

    使用 blake2b 而不是内置 hash()：后者每个进程随机加盐，重启后向量会变。
    采用带符号的桶映射，降低不同特征哈希到同一维度时的相互干扰。
    """
    counter = Counter(_features(text)[:5000])
    vector = [0.0] * dim
    for feature, count in counter.items():
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign * (1.0 + math.log(count))

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


# 混合检索权重：向量语义 55%、BM25 关键词 35%、查询词元覆盖 10%
_VECTOR_WEIGHT = 0.55
_BM25_WEIGHT = 0.35
_LEXICAL_WEIGHT = 0.10

# 查询扩展词表：命中中文术语时补充常见英文/同义表达，提升关键词召回
_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("空值", "空列表", "none", "null", "empty"),
    ("越界", "index", "boundary", "range"),
    ("异常", "exception", "error", "raise"),
    ("注入", "injection", "sql", "command"),
    ("反序列化", "pickle", "deserialize", "yaml"),
    ("密钥", "secret", "api_key", "credential"),
    ("并发", "thread", "lock", "race"),
    ("超时", "timeout", "request"),
    ("测试", "pytest", "test", "coverage"),
    ("可读性", "readability", "naming", "pep8"),
    ("类型", "type", "annotation"),
    ("文件", "file", "path", "open"),
    ("参数", "argument", "parameter"),
    ("返回", "return"),
    ("依赖", "dependency", "requirement"),
)


def expand_queries(query: str, limit: int | None = None) -> list[str]:
    """生成用于多查询检索的变体：原始查询 + 命中的同义词扩展。

    只做确定性的词表扩展，不调用大模型：结果可复现、无额外延迟，
    也不会因为模型措辞漂移导致同一份代码的检索结果不稳定。
    """
    variants = [query.strip()]
    max_variants = settings.RAG_QUERY_VARIANTS if limit is None else limit
    if max_variants <= 1 or not query.strip():
        return [item for item in variants if item]

    lowered = query.lower()
    extra_terms: list[str] = []
    for group in _SYNONYM_GROUPS:
        if any(term in lowered for term in group):
            extra_terms.extend(term for term in group if term not in lowered)
    if extra_terms:
        variants.append(f"{query}\n{' '.join(sorted(set(extra_terms)))}")

    unique: list[str] = []
    for item in variants:
        if item and item not in unique:
            unique.append(item)
    return unique[:max_variants]


class _BM25Index:
    """轻量 BM25 关键词索引，不依赖第三方库，随知识库一起重建。

    BM25 负责精确术语召回（例如 `pickle`、`超时`、`裸 except`），
    向量检索负责语义近似；两者互补后再融合排序。
    """

    def __init__(self, documents: dict[str, list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self._tf: dict[str, Counter[str]] = {
            chunk_id: Counter(terms) for chunk_id, terms in documents.items()
        }
        self._doc_len = {chunk_id: len(terms) for chunk_id, terms in documents.items()}
        total = len(documents)
        self._avg_len = (sum(self._doc_len.values()) / total) if total else 0.0
        doc_freq: Counter[str] = Counter()
        for terms in documents.values():
            doc_freq.update(set(terms))
        self._idf = {
            term: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for term, freq in doc_freq.items()
        }
        self._k1 = k1
        self._b = b

    def scores(self, query_terms: set[str]) -> dict[str, float]:
        """返回每个片段相对该查询的 BM25 原始分数。"""
        if not query_terms or self._avg_len <= 0:
            return {}
        scores: dict[str, float] = {}
        for chunk_id, term_freq in self._tf.items():
            length = self._doc_len[chunk_id]
            score = 0.0
            for term in query_terms:
                freq = term_freq.get(term, 0)
                if not freq:
                    continue
                idf = self._idf.get(term, 0.0)
                denominator = freq + self._k1 * (1 - self._b + self._b * length / self._avg_len)
                score += idf * freq * (self._k1 + 1) / denominator
            scores[chunk_id] = score
        return scores


def _bm25_terms(chunk: _Chunk) -> list[str]:
    """BM25 使用的词元：文档标题 + 小节标题 + 正文。"""
    return _features(f"{chunk.doc_title} {chunk.section}\n{chunk.content}")


def _jaccard(left: set[str], right: set[str]) -> float:
    """两个片段的词元 Jaccard 相似度，用于 MMR 去重。"""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class _HashEmbedding:
    """Chroma 嵌入函数适配器：始终调用本地哈希向量，不下载模型。

    除 __call__ 外还实现 Chroma 1.x 要求的元数据方法（name/get_config 等），
    避免它把自定义函数当成旧版函数而反复告警。
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        """Chroma 要求的调用协议，input 为待向量化的文本列表。"""
        return [_embed_text(text, self.dim) for text in input]

    @staticmethod
    def name() -> str:
        """Chroma 用该方法标识嵌入函数（必须是静态方法，便于按配置重新注册）。"""
        return "local-hash"

    def is_legacy(self) -> bool:
        """本函数实现了完整配置协议，不是旧版嵌入函数。"""
        return False

    def get_config(self) -> dict[str, object]:
        """返回可序列化的配置（Chroma 会写入集合元数据）。"""
        return {"dim": self.dim}

    @staticmethod
    def default_space() -> str:
        """默认使用余弦距离，与本项目“相关度 = 1 - 距离”的口径一致。"""
        return "cosine"

    @staticmethod
    def supported_spaces() -> list[str]:
        """声明支持的向量空间，避免 Chroma 在创建集合时告警。"""
        return ["cosine", "l2", "ip"]

    @staticmethod
    def build_from_config(config: dict[str, object]) -> _HashEmbedding:
        """按配置重建嵌入函数（当前只依赖维度）。"""
        dim = config.get("dim", settings.RAG_EMBED_DIM)
        return _HashEmbedding(dim if isinstance(dim, int) else settings.RAG_EMBED_DIM)

    @staticmethod
    def validate_config(config: dict[str, object]) -> None:
        """配置校验：维度由构造参数保证，无需额外检查。"""
        return None


# ===========================================================================
# 三、从代码里提取风险信号，让检索更贴近具体问题
# ===========================================================================
_SIGNAL_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(eval|exec|compile)\s*\("), "危险函数 代码执行 eval exec"),
    (re.compile(r"os\.(system|popen)|shell\s*=\s*True|subprocess"), "命令注入 子进程 shell"),
    (re.compile(r"pickle\.|marshal\.|yaml\.load\s*\("), "反序列化 不安全加载"),
    (re.compile(r"except\s*:"), "裸 except 异常处理"),
    (re.compile(r"def\s+\w+\([^)]*=\s*(\[\]|\{\}|set\(\))"), "可变默认参数"),
    (re.compile(r"len\s*\(|\bsum\s*\("), "数值边界 除零 除数 空列表"),
    (re.compile(r"\w+\s*\[\s*0\s*\]"), "索引越界 边界判断"),
    (re.compile(r"\b(md5|sha1)\b|random\."), "弱加密 随机数"),
    (re.compile(r"requests\.|urlopen|httpx\."), "网络请求 超时"),
    (re.compile(r"(api_key|password|secret|token)\s*=\s*[\"']"), "硬编码密钥 敏感信息"),
    (
        re.compile(r"(?i)(select|insert|update|delete)[^\n]*[\"']\s*\+"),
        "SQL 注入 字符串拼接 参数化查询",
    ),
    (re.compile(r"(?i)execute\s*\(\s*[^)]*\+"), "SQL 注入 execute 拼接 参数化查询"),
    (re.compile(r"==\s*None|!=\s*None"), "空值 None 判等"),
    (re.compile(r"range\s*\(\s*len\s*\("), "可读性 enumerate 下标"),
    (re.compile(r"\bopen\s*\("), "文件 路径 资源释放"),
    (re.compile(r"if\s+not\s+\w+|len\s*\(\s*\w+\s*\)\s*==\s*0"), "空值 空列表 判断"),
]


def _code_signals(code: str) -> list[str]:
    """扫描代码，返回命中的风险信号描述（用于强化查询语义）。"""
    return [description for pattern, description in _SIGNAL_RULES if pattern.search(code)]


def _code_tokens(code: str, limit: int = 60) -> list[str]:
    """抽取代码中的标识符词元（去重后限量）。

    只把标识符而不是整段源码放进查询：源码里的变量名与符号会稀释查询向量，
    而 eval、pickle、subprocess 这类标识符恰好是规范文档里的关键锚点。
    """
    tokens: set[str] = set()
    for token in _ASCII_TOKEN.findall(code.lower()):
        if len(token) >= 3:
            tokens.add(token)
            for part in token.split("_"):
                if len(part) >= 3:
                    tokens.add(part)
    return sorted(tokens)[:limit]


def build_query(requirement: str, code: str = "") -> str:
    """构造检索查询：需求原文 + 代码风险信号 + 代码标识符词元。"""
    parts: list[str] = [requirement.strip()[:300]]
    signals = _code_signals(code)
    if signals:
        parts.append("；".join(signals))
    tokens = _code_tokens(code)
    if tokens:
        parts.append(" ".join(tokens))
    return "\n".join(part for part in parts if part)


def _lexical_score(query_features: set[str], chunk_features: set[str]) -> float:
    """查询词元覆盖率：查询里有多少比例的词元在该片段中出现（0~1）。

    它作为向量召回的补充：哈希向量擅长模糊匹配，词元覆盖率擅长精确命中
    规范条文里的同名术语（如“越界”“裸 except”“超时”）。
    """
    if not query_features or not chunk_features:
        return 0.0
    return len(query_features & chunk_features) / len(query_features)


# ===========================================================================
# 四、检索服务
# ===========================================================================
class RagRetriever:
    """Chroma 向量检索器（进程内单例，索引懒加载、按需重建）。"""

    def __init__(self) -> None:
        self.available = False
        self.error = ""
        self.chunk_count = 0
        self._collection = None
        # 片段词元缓存：精排与 MMR 去重时避免重复切词
        self._chunk_features: dict[str, set[str]] = {}
        # BM25 关键词索引：与向量索引一同重建
        self._bm25: _BM25Index | None = None
        self._lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------------
    # 索引构建
    # ------------------------------------------------------------------
    def ensure_index(self) -> bool:
        """确保索引可用；首次调用建库，失败后的后续调用允许重试。"""
        if self._initialized and self.available:
            return True
        with self._lock:
            if self._initialized and self.available:
                return True
            self._initialized = True
            return self._build_safely(action="索引初始化")

    def refresh(self) -> bool:
        """上传附件入库后强制比对内容指纹，必要时重建 Chroma 集合。"""
        with self._lock:
            self._initialized = True
            return self._build_safely(action="索引刷新")

    def _build_safely(self, action: str) -> bool:
        """执行索引构建并统一维护可用状态。

        调用方必须已经持有 ``self._lock``。RAG 是增强能力，构建失败时只降级
        检索结果，不能让代码生成、审核或附件上传主链路一起失败。
        """
        try:
            self._build()
            self.available = True
            self.error = ""
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.available = False
            print(f"[RAG] {action}失败，检索降级为空：{self.error}")
        return self.available

    def _build(self) -> None:
        """读取知识库、比对指纹，必要时重建 Chroma 集合。"""
        import chromadb
        from chromadb.config import Settings

        chunks = _load_chunks()
        if not chunks:
            raise RuntimeError("knowledge 目录下没有可索引的 markdown 文档")
        self.chunk_count = len(chunks)
        self._chunk_features = {
            chunk.chunk_id: set(_features(f"{chunk.doc_title} {chunk.section}\n{chunk.content}"))
            for chunk in chunks
        }
        self._bm25 = _BM25Index({chunk.chunk_id: _bm25_terms(chunk) for chunk in chunks})
        fingerprint = _fingerprint(chunks)

        client = chromadb.PersistentClient(
            path=str(settings.RAG_DIR),
            settings=Settings(anonymized_telemetry=False),
        )
        embedding = _HashEmbedding(settings.RAG_EMBED_DIM)
        name = settings.RAG_COLLECTION

        manifest_path = settings.RAG_DIR / "manifest.json"
        manifest = {}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = {}

        # 指纹一致且条数吻合时直接复用已有索引
        if manifest.get("fingerprint") == fingerprint:
            collection = client.get_collection(name, embedding_function=embedding)
            if collection.count() == len(chunks):
                self._collection = collection
                return

        # 否则删除重建（知识库更新后索引必须与内容一致）
        if name in {item.name for item in client.list_collections()}:
            client.delete_collection(name)
        collection = client.create_collection(
            name=name,
            embedding_function=embedding,
            metadata={"hnsw:space": "cosine"},
        )
        collection.add(
            ids=[chunk.chunk_id for chunk in chunks],
            documents=[chunk.content for chunk in chunks],
            metadatas=[
                {
                    "doc": chunk.doc,
                    "doc_title": chunk.doc_title,
                    "section": chunk.section,
                }
                for chunk in chunks
            ],
            embeddings=[
                _embed_text(
                    f"{chunk.doc_title} {chunk.section}\n{chunk.content}",
                    settings.RAG_EMBED_DIM,
                )
                for chunk in chunks
            ],
        )
        manifest_path.write_text(
            json.dumps(
                {"fingerprint": fingerprint, "chunks": len(chunks), "version": _INDEX_VERSION},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._collection = collection

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def _vector_hits(
        self, query: str, n_results: int
    ) -> dict[str, tuple[str, dict[str, object], float]]:
        """查询向量库，返回 {chunk_id: (正文, 元数据, 向量相似度)}。"""
        if self._collection is None:
            return {}
        result = self._collection.query(
            query_embeddings=[_embed_text(query, settings.RAG_EMBED_DIM)],
            # 知识库规模较小，直接取全量候选，保证 BM25 命中的片段也能拿到向量分
            n_results=max(1, n_results),
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits: dict[str, tuple[str, dict[str, object], float]] = {}
        for chunk_id, document, metadata, distance in zip(
            ids, documents, metadatas, distances, strict=False
        ):
            hits[chunk_id] = (
                document or "",
                metadata or {},
                max(0.0, 1.0 - float(distance)),
            )
        return hits

    def _mmr_select(self, scored: list[tuple[float, RagHit]], k: int) -> list[tuple[float, RagHit]]:
        """用 MMR 在相关度与多样性之间取平衡，避免返回多个近重复片段。"""
        selected: list[tuple[float, RagHit]] = []
        remaining = list(scored)
        weight = min(1.0, max(0.0, settings.RAG_MMR_LAMBDA))
        while remaining and len(selected) < k:
            best_index = 0
            best_value = float("-inf")
            for index, (score, hit) in enumerate(remaining):
                features = self._chunk_features.get(hit.chunk_id, set())
                if selected:
                    similarity = max(
                        _jaccard(features, self._chunk_features.get(item.chunk_id, set()))
                        for _, item in selected
                    )
                else:
                    similarity = 0.0
                value = weight * score - (1 - weight) * similarity
                if value > best_value:
                    best_value = value
                    best_index = index
            selected.append(remaining.pop(best_index))
        return selected

    def search(
        self,
        query: str,
        top_k: int | None = None,
        min_score: float | None = None,
        doc_filter: str | None = None,
        mode: str = "hybrid",
    ) -> list[RagHit]:
        """混合检索知识片段；任何异常都降级为空列表。

        :param mode: vector=仅向量；hybrid_no_mmr=向量+BM25+查询扩展；
                     hybrid=在 hybrid_no_mmr 基础上再做 MMR 多样性重排。
        """
        if not query.strip() or not self.ensure_index() or self._collection is None:
            return []
        if mode not in {"vector", "hybrid_no_mmr", "hybrid"}:
            mode = "hybrid"
        k = max(1, top_k or settings.RAG_TOP_K)
        threshold = settings.RAG_MIN_SCORE if min_score is None else min_score
        queries = [query] if mode == "vector" else expand_queries(query)
        total = self._collection.count()
        if total <= 0:
            return []

        # 不同查询变体各自打分，同一片段取最高分
        best: dict[str, tuple[float, str, dict[str, object]]] = {}
        try:
            for variant in queries:
                vector_hits = self._vector_hits(variant, total)
                query_features = set(_features(variant))
                bm25_raw: dict[str, float] = {}
                max_bm25 = 0.0
                if mode != "vector" and self._bm25 is not None:
                    bm25_raw = self._bm25.scores(query_features)
                    max_bm25 = max(bm25_raw.values(), default=0.0)

                for chunk_id, (document, metadata, vector_score) in vector_hits.items():
                    lexical = _lexical_score(
                        query_features, self._chunk_features.get(chunk_id, set())
                    )
                    if mode == "vector":
                        relevance = vector_score
                    else:
                        bm25_score = bm25_raw.get(chunk_id, 0.0) / max_bm25 if max_bm25 > 0 else 0.0
                        relevance = (
                            _VECTOR_WEIGHT * vector_score
                            + _BM25_WEIGHT * bm25_score
                            + _LEXICAL_WEIGHT * lexical
                        )
                    relevance = round(relevance, 4)
                    if relevance < threshold:
                        continue
                    if doc_filter and metadata.get("doc") != doc_filter:
                        continue
                    current = best.get(chunk_id)
                    if current is None or relevance > current[0]:
                        best[chunk_id] = (relevance, document, metadata)
        except Exception as exc:
            print(f"[RAG] 检索失败，降级为空：{type(exc).__name__}: {exc}")
            return []

        scored: list[tuple[float, RagHit]] = []
        for chunk_id, (relevance, document, metadata) in best.items():
            scored.append(
                (
                    relevance,
                    RagHit(
                        chunk_id=chunk_id,
                        doc=str(metadata.get("doc", "")),
                        doc_title=str(metadata.get("doc_title", "")),
                        section=str(metadata.get("section", "")),
                        content=document.strip(),
                        score=relevance,
                    ),
                )
            )

        scored.sort(key=lambda item: item[0], reverse=True)
        if mode == "hybrid":
            scored = self._mmr_select(scored, k)
        return [hit for _, hit in scored[:k]]

    def stats(self) -> dict[str, object]:
        """返回索引状态，供健康检查与调试使用。"""
        return {
            "available": self.available,
            "engine": "chroma",
            "chunks": self.chunk_count,
            "embedding": f"local-hash-{settings.RAG_EMBED_DIM}d",
            "retrieval": "vector+bm25+query-expansion+mmr",
            "index_version": _INDEX_VERSION,
            "dir": str(settings.RAG_DIR),
            "error": self.error,
        }


# 模块级单例：整个应用共用一个索引与集合句柄
rag_retriever = RagRetriever()
