"""RAG 检索层的回归测试：索引可建、命中来源正确、混合检索与评估指标可用。"""

import math

from models.chat_schemas import ConfirmedRequirement, RagHit
from prompts.chat_prompts import generate_solution_prompt, review_code_prompt
from services.rag_eval import ndcg_at_k, recall_at_k, reciprocal_rank
from services.rag_retriever import build_query, expand_queries, rag_retriever


def test_index_builds_and_counts_chunks() -> None:
    """索引应能从 knowledge 目录建好，且片段数大于 0。"""
    assert rag_retriever.ensure_index() is True
    assert rag_retriever.chunk_count > 0


def test_eval_code_hits_security_doc() -> None:
    """含 eval 的代码应检索到安全规范，而不是无关文档。"""
    hits = rag_retriever.search(
        build_query("执行传入的表达式", "def run(cmd):\n    return eval(cmd)")
    )
    assert hits, "检索结果不应为空"
    assert hits[0].doc == "security"
    assert hits[0].score > 0


def test_boundary_code_hits_boundary_docs() -> None:
    """除法与空列表场景应命中边界类规范。"""
    query = build_query(
        "求平均值，空列表返回 0", "def avg(nums):\n    return sum(nums) / len(nums)"
    )
    hits = rag_retriever.search(query)
    assert hits
    docs = {hit.doc for hit in hits}
    assert "logic_boundary" in docs or "testing" in docs


def test_empty_query_returns_empty() -> None:
    """空查询直接返回空列表，不触发检索异常。"""
    assert rag_retriever.search("") == []


def test_hit_carries_source_metadata() -> None:
    """每条命中都必须带文档、小节与相关度，供前端展示证据。"""
    hits = rag_retriever.search(
        build_query("写 pytest 测试", "def test_add():\n    assert add(1, 2)")
    )
    assert hits
    for hit in hits:
        assert hit.chunk_id
        assert hit.doc
        assert hit.section
        assert 0.0 <= hit.score <= 1.0


def test_review_prompt_contains_retrieved_knowledge() -> None:
    """检索命中必须真正进入审核提示词，而不只是返回给前端。"""
    code = "def run(cmd):\n    return eval(cmd)"
    hits = rag_retriever.search(build_query("执行表达式", code))
    assert hits
    system, _user = review_code_prompt(code, "执行表达式", "", hits)
    assert "知识库命中条目" in system
    assert hits[0].section in system


def test_generate_prompt_contains_retrieved_knowledge() -> None:
    """生成环节同样要把检索命中的规范注入提示词。"""
    hits = rag_retriever.search(
        build_query("求列表平均值", "def avg(nums):\n    return sum(nums) / len(nums)")
    )
    assert hits
    confirmed = ConfirmedRequirement(
        function_name="average",
        restated_requirement="求列表平均值，空列表返回 0",
    )
    system, _user = generate_solution_prompt(confirmed, hits)
    assert "知识库命中条目" in system


def test_index_rebuilds_after_knowledge_changes(tmp_path, monkeypatch) -> None:
    """知识库内容变化后，索引应自动重建，而不是沿用旧内容。"""
    import services.rag_retriever as rag_module

    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    doc = knowledge / "sample.md"
    doc.write_text("# 示例规范\n\n## 第一节\n内容一\n", encoding="utf-8")

    monkeypatch.setattr(rag_module.settings, "KNOWLEDGE_DIR", knowledge)
    monkeypatch.setattr(rag_module.settings, "RAG_DIR", tmp_path / "rag")
    monkeypatch.setattr(rag_module.settings, "RAG_COLLECTION", "test_kb")

    first = rag_module.RagRetriever()
    assert first.ensure_index() is True
    assert first.chunk_count == 1

    doc.write_text("# 示例规范\n\n## 第一节\n内容一\n\n## 第二节\n内容二\n", encoding="utf-8")
    second = rag_module.RagRetriever()
    assert second.ensure_index() is True
    assert second.chunk_count == 2


def test_query_expansion_adds_english_synonyms() -> None:
    """中文术语应扩展出英文/同义表达，供 BM25 精确召回。"""
    variants = expand_queries("空列表和 None 怎么处理")
    assert len(variants) == 2
    assert "none" in variants[1].lower()
    assert "empty" in variants[1].lower()


def test_hybrid_search_finds_sql_injection_clause() -> None:
    """关键词明确的查询应由混合检索稳定命中对应规范。"""
    hits = rag_retriever.search("SQL 查询把用户输入直接拼进语句", min_score=0.0)
    assert hits
    assert hits[0].chunk_id == "security_review_cases#3"


def test_sql_concat_code_signal_hits_parameterized_clause() -> None:
    """代码里出现 SQL 字符串拼接时，查询信号应把参数化查询条目顶上来。"""
    code = (
        "def query_user(conn, name):\n"
        '    sql = "SELECT * FROM users WHERE name = \'" + name + "\'"\n'
        "    return conn.execute(sql)\n"
    )
    hits = rag_retriever.search(build_query("检查 SQL 注入风险", code), top_k=5, min_score=0.0)
    assert "security_review_cases#3" in {hit.chunk_id for hit in hits}


def test_vector_mode_still_available() -> None:
    """保留纯向量模式用于消融评估。"""
    hits = rag_retriever.search("pickle 反序列化", mode="vector", min_score=0.0)
    assert hits
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)


def test_stats_exposes_retrieval_strategy() -> None:
    """健康检查应能看到当前检索策略与索引版本。"""
    assert rag_retriever.ensure_index() is True
    stats = rag_retriever.stats()
    assert "bm25" in str(stats["retrieval"])
    assert stats["chunks"] > 0


def test_eval_metrics_on_known_hits() -> None:
    """Recall/MRR/nDCG 的口径应可手工验证。"""

    def hit(chunk_id: str) -> RagHit:
        return RagHit(
            chunk_id=chunk_id,
            doc="doc",
            doc_title="title",
            section="section",
            content="content",
            score=1.0,
        )

    hits = [hit("a"), hit("b"), hit("c")]
    assert recall_at_k(hits, ("b",), 3) == 1.0
    assert reciprocal_rank(hits, ("b",)) == 0.5
    assert ndcg_at_k(hits, ("b",), 3) == 1 / math.log2(3)
    assert recall_at_k(hits, ("z",), 3) == 0.0
