"""RAG 检索评估命令：python -m scripts.evaluate_rag。

输出三种模式在标注查询集上的 Recall@K / MRR / nDCG@K，
并把逐条明细写入 storage/rag_eval_report.json。
"""

from __future__ import annotations

import json
from pathlib import Path

from services.rag_eval import load_cases, run_evaluation

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "scripts" / "rag_eval_cases.json"
REPORT_PATH = ROOT / "storage" / "rag_eval_report.json"


def main() -> None:
    """读取标注集、跑三种检索模式并打印对比表。"""
    cases = load_cases(CASES_PATH)
    report = run_evaluation(cases)
    top_k = report["top_k"]
    print(f"知识库片段数：{report['chunk_count']}，标注查询数：{len(cases)}，K={top_k}")
    print(f"{'模式':<16}{'Recall@K':>10}{'MRR':>10}{'nDCG@K':>10}{'冗余度':>10}")
    for mode in ("vector", "hybrid_no_mmr", "hybrid"):
        metrics = report["modes"][mode]
        print(
            f"{mode:<16}{metrics['recall']:>10.3f}"
            f"{metrics['mrr']:>10.3f}{metrics['ndcg']:>10.3f}"
            f"{metrics['redundancy']:>10.4f}"
        )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"逐条明细已写入：{REPORT_PATH}")


if __name__ == "__main__":
    main()
