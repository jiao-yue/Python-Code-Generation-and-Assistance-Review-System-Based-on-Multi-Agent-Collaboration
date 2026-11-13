"""离线评估 RAG 检索质量：Recall@K、MRR、nDCG@K。

不依赖在线模型或额外评估库，直接用标注查询集跑当前知识库，
便于对比“纯向量 / 混合检索 / 混合+MMR”三种模式的效果。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

from models.chat_schemas import RagHit
from services.rag_retriever import RagRetriever, _features, _jaccard, rag_retriever


@dataclass(frozen=True)
class EvalCase:
    """一条标注查询：query 为检索词，expected 为应命中的片段 ID。"""

    query: str
    expected: tuple[str, ...]


def load_cases(path: Path) -> list[EvalCase]:
    """从 JSON 读取标注查询集。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [EvalCase(query=str(item["query"]), expected=tuple(item["expected"])) for item in raw]


def _first_relevant_rank(hits: list[RagHit], expected: tuple[str, ...]) -> int:
    """返回第一个命中片段的 1-based 排名；没有命中返回 0。"""
    for rank, hit in enumerate(hits, start=1):
        if hit.chunk_id in expected:
            return rank
    return 0


def recall_at_k(hits: list[RagHit], expected: tuple[str, ...], k: int) -> float:
    """Top-K 内是否至少命中一个相关片段。"""
    return 1.0 if any(hit.chunk_id in expected for hit in hits[:k]) else 0.0


def reciprocal_rank(hits: list[RagHit], expected: tuple[str, ...]) -> float:
    """第一个相关片段排名的倒数（MRR 的单条贡献）。"""
    rank = _first_relevant_rank(hits, expected)
    return 1.0 / rank if rank else 0.0


def ndcg_at_k(hits: list[RagHit], expected: tuple[str, ...], k: int) -> float:
    """单条相关片段的 nDCG@K：越靠前越接近 1。"""
    rank = _first_relevant_rank(hits, expected)
    if not rank or rank > k:
        return 0.0
    return 1.0 / math.log2(rank + 1)


def average_pairwise_similarity(hits: list[RagHit]) -> float:
    """Top-K 片段两两词元 Jaccard 的均值，用于衡量结果冗余度。"""
    if len(hits) < 2:
        return 0.0
    similarities = [
        _jaccard(set(_features(left.content)), set(_features(right.content)))
        for left, right in combinations(hits, 2)
    ]
    return sum(similarities) / len(similarities)


def evaluate_mode(
    retriever: RagRetriever,
    cases: list[EvalCase],
    mode: str,
    top_k: int = 4,
) -> dict[str, Any]:
    """跑一种检索模式，返回平均指标与逐条明细。"""
    rows: list[dict[str, Any]] = []
    for case in cases:
        hits = retriever.search(case.query, top_k=top_k, min_score=0.0, mode=mode)
        rank = _first_relevant_rank(hits, case.expected)
        rows.append(
            {
                "query": case.query,
                "expected": list(case.expected),
                "rank": rank,
                "hit": hits[0].chunk_id if rank == 1 else (hits[rank - 1].chunk_id if rank else ""),
                "recall": recall_at_k(hits, case.expected, top_k),
                "rr": reciprocal_rank(hits, case.expected),
                "ndcg": ndcg_at_k(hits, case.expected, top_k),
                "redundancy": round(average_pairwise_similarity(hits[:top_k]), 4),
            }
        )
    count = len(rows) or 1
    return {
        "mode": mode,
        "cases": len(rows),
        "recall": round(sum(row["recall"] for row in rows) / count, 4),
        "mrr": round(sum(row["rr"] for row in rows) / count, 4),
        "ndcg": round(sum(row["ndcg"] for row in rows) / count, 4),
        "redundancy": round(sum(row["redundancy"] for row in rows) / count, 4),
        "details": rows,
    }


def run_evaluation(
    cases: list[EvalCase],
    top_k: int = 4,
    retriever: RagRetriever | None = None,
) -> dict[str, Any]:
    """依次评估三种模式并返回完整报告。"""
    target = retriever or rag_retriever
    modes = ("vector", "hybrid_no_mmr", "hybrid")
    report = {mode: evaluate_mode(target, cases, mode, top_k) for mode in modes}
    return {"top_k": top_k, "chunk_count": target.chunk_count, "modes": report}
