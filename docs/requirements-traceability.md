# 开题报告要求与实现对照

本表用于把开题报告中的功能要求落到具体代码、数据产物和验证命令，避免“文档写了但系统没有”。

| 开题报告要求 | 实现位置 | 验证证据 |
| --- | --- | --- |
| 会话结果可导出、可复现 | `services/export_service.py`、`api/server.py` 的 `/api/conversations/{id}/export`、`web/app.js` 导出按钮 | `tests/test_export.py` 5 项；ZIP 含 `manifest.json`、`requirement.md`、`versions/round_XX_vYY/`、`final/`；`scope=latest/all` |
| HumanEval、MBPP 与自建任务集 | `evaluation/build_datasets.py`、`evaluation/datasets.py`、`evaluation/datasets/*.jsonl` | 15 + 15 + 10 = 40 题；`python -m evaluation.build_datasets --validate` 全部参考实现通过官方测试 |
| G1–G4 消融实验 | `evaluation/runner.py`、`evaluation/run_ablation.py`、`evaluation/metrics.py` | 每组语义固定；`--limit/--groups/--samples/--output-dir` 可复现；真实 G1、G4 各 1 题 smoke 已通过 |
| 缺陷检出率与误报率 | `evaluation/build_defect_cases.py`、`evaluation/defect_eval.py`、`evaluation/run_defects.py` | `evaluation/defect_sets/cases20.jsonl` 共 20 buggy + 20 clean；按行号 ±1 且视角一致匹配；FPR 只计阻断/主要 |
| 实验数据持久化 | `evaluation/store.py` | MySQL `experiment_batches`、`experiment_runs`、`defect_metrics`；列名使用 `group_list`，避开保留字 `groups` |
| 三视角并行审查 | `prompts/chat_prompts.py`、`services/chat_llm.py`、`graph/chat_graph.py` | spec/security/logic 三路 `ThreadPoolExecutor`；单路失败保留其余结果；三路全失败回退串行；`tests/test_parallel_review.py` 3 项 |
| 静态工具并行 | `services/chat_workspace.py` | Ruff、Bandit、ast 三工具并行；`REVIEW_PARALLEL=0` 回退顺序执行 |
| 问题去重与定向修复 | `graph/chat_graph.py` 的 `_dedupe_review_items`、`_assemble_report` | 按“行号 + 问题类型”合并，保留更严重、描述更完整的一条；再过滤低置信度、评分 |
| FastAPI + SSE + 原生 Web | `api/server.py`、`web/index.html`、`web/app.js`、`web/styles.css` | `GET /api/health` 返回 `status=ok`；首页 200，静态资源 `v=19`；SSE `stage` 携带 `partial` 增量结果、前端逐行揭示；用户右/助手左；浏览器地址 `http://127.0.0.1:8000` |
| 本地 Ollama 模型调用 | `config/settings.py`、`services/chat_llm.py`、`api/server.py`、`web/app.js` | `LLM_PROVIDER=ollama` 时走原生 `/api/chat`，无 Key 仍在线；`/api/health` 返回 `mode=local`；简单函数实测 `COMPLETED`、5/5 测试通过；`tests/test_local_provider.py` 2 项 |
| MySQL 会话与过程留痕 | `services/chat_memory.py`、`evaluation/store.py` | 新建会话即落库；刷新不丢失；实验批次与逐题/缺陷指标可写入 |
| README 与开题报告同步 | `README.md`、`evaluation/README.md`、`evaluation/datasets/SOURCES.md`、开题报告 DOCX | README 已覆盖导出、评测、消融、并行审查、实验表、Ollama 接入、77 项测试；开题报告已改为 FastAPI/MySQL/G1–G4/40 题/三路并行 |

## 当前未执行的全量实验

- 真实 40 题 × G1–G4 共 160 次生成/修复会消耗较多 API 额度和时间，需单独确认后再跑：
  `python -m evaluation.run_ablation`。
- 真实 LLM 缺陷评测需在 `FORCE_MOCK=0` 下运行：
  `python -m evaluation.run_defects`。
- 离线结果只用于链路验证，不作为论文结论；真实结果应以 `online_` 前缀批次为准。
