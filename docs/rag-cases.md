# RAG 案例调研与落地记录

> 调研时间：2026-11-13
> 调研方式：GitHub Search API（按 star 排序）检索 `RAG techniques`、`rag from scratch`、`hybrid search RAG`、`code review guidelines` 等关键词，再读取仓库 README、官方文档与代码评审/安全规范原文。
> 本文记录“参考了什么、落地到哪里、实测效果如何”，对应实现见 `services/rag_retriever.py` 与 `knowledge/`。

## 一、参考案例

| 仓库 | 规模 | 借鉴内容 | 本项目落地 |
|---|---|---|---|
| [NirDiamant/RAG_Techniques](https://github.com/NirDiamant/RAG_Techniques) | 约 3.0 万星、42 个教程 | Fusion Retrieval（关键词+向量）、Reranking、Contextual Chunk Headers、RAG 评估 | 混合检索、片段头上下文、评估脚本 |
| [langchain-ai/rag-from-scratch](https://github.com/langchain-ai/rag-from-scratch) | 约 1.0 万星 | Query Translation / RAG-Fusion、多查询召回 | 规则化查询扩展 + 多查询取最高分 |
| [chroma-core/chroma](https://github.com/chroma-core/chroma) | Chroma 官方仓库 | PersistentClient、集合元数据、余弦空间 | 沿用现有持久化集合与指纹重建 |
| [vibrantlabsai/ragas](https://github.com/vibrantlabsai/ragas) | 约 1.6 万星 | Recall / MRR / nDCG 等检索评估口径 | 无额外依赖的离线评估实现 |
| [google/eng-practices](https://github.com/google/eng-practices) | Google 官方代码评审规范 | 评审优先级、设计/功能/复杂度/测试检查项、评论粒度 | `knowledge/code_review_cases.md` |
| [OWASP/CheatSheetSeries](https://github.com/OWASP/CheatSheetSeries) | OWASP 官方安全手册 | 输入校验、SQL 注入、命令注入、日志与依赖安全 | `knowledge/security_review_cases.md` |

## 二、逐项落地

### 1. 混合检索：向量 + BM25 + 词元覆盖

参考 `RAG_Techniques` 的 Fusion Retrieval：关键词检索擅长精确命中术语（`pickle`、`裸 except`、`超时`），向量检索擅长语义近似，两者融合比单一向量更稳。

实现：

- `_BM25Index`：本地 BM25（`k1=1.5`、`b=0.75`），随知识库一起重建，不引入第三方依赖。
- 每个候选片段的最终相关度：
  `0.55 × 向量余弦 + 0.35 × 归一化 BM25 + 0.10 × 查询词元覆盖率`
- 知识库只有几十个片段，直接对全量片段计算 BM25 与向量分，保证关键词命中的片段也能拿到向量分。

### 2. 查询扩展：确定性同义词

参考 `rag-from-scratch` 的 Query Translation / RAG-Fusion，但不调用大模型改写查询：

- `expand_queries()` 命中中文术语时补充英文/同义表达，例如“空列表 → none/null/empty”“注入 → injection/sql/command”。
- 一次检索最多使用 2 个查询变体，同一片段取最高分。
- 选择规则化扩展的原因：结果可复现、无额外延迟、不依赖 DeepSeek 是否支持 embedding 或改写接口。

### 3. 片段头上下文

参考 Contextual Chunk Headers：把文档标题、小节标题与正文一起参与向量化、BM25 与词元覆盖计算，避免脱离文档后代词与术语失去上下文。

### 4. MMR 多样性重排

参考 Reranking / MMR 思路，在相关度排序后做 Maximal Marginal Relevance：

- `score = 0.85 × 相关度 - 0.15 × 与已选片段的最大词元相似度`
- 降低近重复片段同时进入提示词的概率，默认 λ=0.85，避免过度牺牲相关度。

### 5. 检索评估

参考 `ragas` 与 `RAG_Techniques` 的评估章节，新增不依赖在线模型的离线评估：

- 指标：Recall@K、MRR、nDCG@K、冗余度（Top-K 两两词元 Jaccard 均值）。
- 标注集：`scripts/rag_eval_cases.json`，20 条查询覆盖规范、安全、边界、测试与评审场景。
- 三种模式对比：`vector`、`hybrid_no_mmr`、`hybrid`。

### 6. 代码信号补充查询

审核场景的查询不只有用户填写的上下文，`build_query()` 还会从待审代码里抽取风险信号：

- `eval` / `exec` → 危险代码执行
- SQL 字符串拼接（`"SELECT ... '" + name`）→ SQL 注入、字符串拼接、参数化查询
- `pickle.loads` → 反序列化
- `subprocess` / `shell=True` → 命令注入
- 裸 `except`、`== None`、可变默认参数等 → 边界与异常处理

这些信号会拼进检索查询，保证“代码里已经出现的风险”能对应到知识库里的规范条目；实测中 SQL 拼接代码已能稳定命中 `security_review_cases#3`。

生成阶段的查询也不再只取 `restated_requirement`：`_generation_query()` 会把输入、输出、边界、约束、验收标准和假设一起拼入查询，避免“禁用 eval”“文件路径”“空列表”等约束在生成时漏检。

实测结果（知识库 41 个片段，K=4）：

| 模式 | Recall@4 | MRR | nDCG@4 | 冗余度 |
|---|---:|---:|---:|---:|
| vector | 0.750 | 0.725 | 0.732 | 0.0215 |
| hybrid_no_mmr | 1.000 | 0.942 | 0.957 | 0.0284 |
| hybrid（含 MMR） | 1.000 | 0.942 | 0.957 | 0.0242 |

结论：混合检索把 Recall@4 从 0.750 提升到 1.000，MRR 从 0.725 提升到 0.942；MMR 在不降低 Recall/MRR/nDCG 的前提下，把 Top-4 的平均两两相似度从 0.0284 降到 0.0242。

复现命令：

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate_rag
```

输出：

- 控制台对比表
- `storage/rag_eval_report.json`（含逐条查询的排名与命中片段）

## 三、为什么没有采用这些方案

| 方案 | 未采用原因 |
|---|---|
| 在线 embedding（OpenAI/BGE） | DeepSeek 默认不提供 embedding 端点；引入模型下载或外部 API 会增加部署与演示不确定性 |
| LLM 查询重写 / HyDE | 每次检索多一次模型调用，延迟与费用上升，同一代码两次检索结果可能不一致 |
| Cross-Encoder 重排 | 需要额外模型与推理开销；当前 41 个片段的规模，BM25+词元覆盖已足够 |
| RAGAS 依赖 | 评估指标可本地计算，避免为一个脚本增加较重依赖；答辩环境更易复现 |
| 语义切分 | 现有知识库按二级标题切分，本身已是语义段落，重切收益有限 |

## 四、知识库新增内容

| 文件 | 片段数 | 内容 |
|---|---:|---|
| `knowledge/code_review_cases.md` | 8（含概述） | 评审目标、设计、功能与并发、复杂度、测试有效性、命名注释、评论粒度 |
| `knowledge/security_review_cases.md` | 6（含概述） | 输入校验、SQL 注入、命令与路径注入、输出与日志、依赖与权限 |

知识库从 27 个片段增加到 41 个片段；`storage/rag/manifest.json` 会因内容指纹变化自动重建索引，无需手动刷新。
