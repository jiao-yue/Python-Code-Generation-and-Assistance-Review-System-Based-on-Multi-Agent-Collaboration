# 基于多智能体协同的 Python 代码生成与辅助审查系统

一套以“**确定性静态工具 + 多智能体 LLM + 可执行测试 + RAG 规范检索**”为核心的代码生成与审查系统，
在原有 Python 单函数链路上扩展 Python / Java / C / HTML 多语言、多文件小型项目生成能力。

- **多语言生成**：按需求自动识别并生成 Python、Java、C、HTML（含 CSS/JavaScript）代码；
- **多文件项目**：不局限于单个函数，可生成 2~12 个文件的小型工程，附带 README、入口文件与运行命令；
- **生成闭环**：需求确认 → 需求不明确时中断提问 → 按需求契约生成代码/项目 → 静态工具与 LLM 联合审核
  → 按语言执行 pytest/javac/gcc/HTML 静态检查 → 未通过时定向修复；
- **只审核代码**：粘贴已有代码（可附测试），只做静态审核与测试，不生成、不改动用户代码；
- **附件材料**：上传 Word、Excel、PDF、图片或文本/代码附件，可用于当前对话，也可显式导入 RAG 知识库；
  原始文件按完整字节 MD5 去重，图片与扫描 PDF 使用本地 OCR 提取文字。

后端用 **FastAPI** 搭建，核心流程由 **LangGraph** 状态图编排，提示词采用 **few-shot 示例模式**，
结果以 **SSE 增量流式**返回：每个节点完成即推送当前进度（`partial`），前端边收边逐行揭示，
收尾后切换为完整答案；会话与每轮消息持久化到 **MySQL**（新建即落库、刷新不丢失、
删除即同步删库、消息编号连续无空洞）。前端为仿豆包聊天界面：用户消息在右、助手答案在左，
答案为 Markdown 富文本（标题、列表、代码块、表格），纯文字、无图片/logo，无需 Node 构建。

---

## 快速使用

完整分步说明、模型切换、附件示例和故障排查见 **[项目使用手册](docs/user-manual.md)**。

### 1. 安装并配置

```powershell
cd "项目根目录"
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

在 `.env` 中配置 MySQL 口令和一种模型服务。默认使用 DeepSeek；使用本机 Ollama 时设置：

```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:4b
REVIEW_PARALLEL=0
```

暂时没有模型服务时可设置 `FORCE_MOCK=1` 离线演示。修改 `.env` 后需重启服务。

### 2. 启动系统

```powershell
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000/`，并通过 `http://127.0.0.1:8000/api/health` 检查模型、MySQL 和 RAG 状态。

### 3. 生成或审核代码

- **代码生成**：新建对话 → 选择“代码生成” → 输入语言、功能、输入输出、边界和文件要求 → 发送；
  若出现“需求需要确认”，填写问答卡并点击“提交回答并继续”。完成后查看代码、运行命令、审核报告和测试结果。
- **只审核代码**：切换到“只审核代码” → 粘贴 Python 代码或添加代码附件 → 可选粘贴 pytest 测试 → 发送；
  系统只审核和测试，不生成或改写用户代码。
- **附件**：一次性材料点击“添加附件”；需要长期检索的规范或案例点击“导入知识库”。
  单次最多 5 个文件，单文件默认最大 20 MB。
- **保存结果**：点击结果区域的“导出结果 ZIP”。历史会话支持重命名、继续查看和删除；删除会同步删除 MySQL 记录。
- **运行生成项目**：按结果中的入口文件和 `run_command`，在 `storage/generated/chat_<run_id>/` 中运行；
  多文件网页需保持 HTML/CSS/JS 的相对位置。

---

## 一、系统能力

| 能力 | 说明 |
| --- | --- |
| 需求确认 | LLM 把口语化需求整理为结构化需求：函数名/入口名、输入输出、边界、约束、验收标准与假设 |
| 语言识别 | 从需求关键词识别 Python / Java / C / HTML；HTML 项目自动拆分为 HTML、CSS、JavaScript 文件 |
| 中断澄清 | 只有会改变实现结果的歧义才提问（最多 3 条）；LangGraph `interrupt()` 挂起，`/api/chat/resume` 恢复 |
| 单文件生成 | Python 函数题生成 `solution.py` + `test_solution.py`，保持原 HumanEval/MBPP 评测链路 |
| 多文件生成 | 先规划文件清单，再逐文件生成；每个文件独立输出纯文本，避免长 JSON 被截断 |
| 项目可运行 | Java 用 `javac` 编译并运行无输入入口、C 用 `gcc` 编译链接并运行无输入程序、HTML 检查标签/本地 CSS/JS 引用、JS 用 `node --check`、Python 用 `pytest` |
| 三路并行审查 | spec/security/logic 三个独立 LLM 调用并行执行，Ruff/Bandit/ast 也并行；合并后按“文件+行号+类型”去重 |
| 测试失败闭环 | pytest/编译检查失败会转成阻断级审核问题，修复节点能拿到失败用例与 traceback，不再空转 |
| 确定性评分卡 | 5 维度 0–10 量化、加权总分与 A–F 等级、质量门禁、优先改进动作；同样输入结果可复现 |
| 低置信度过滤 | LLM 问题带置信度，低于阈值自动过滤；静态工具与 pytest 的确定性结果全部保留 |
| 可执行测试 | pytest 子进程隔离执行，精确统计通过/失败并提取 traceback；每轮清理旧报告与旧生成文件 |
| 收敛判定 | 无阻断问题且项目检查全绿即收敛，否则进入定向修复闭环（最多 `MAX_ROUNDS` 轮） |
| 定向修复 | 按问题清单做最小化补丁，重新生成多文件项目，保留可回退版本 |
| 结果导出 | 会话结果一键导出 ZIP，包含需求、各轮 `files/` 项目文件、审核报告、检查结果与 manifest |
| 流式输出 | 每个节点完成即推送 `partial` 当前进度，前端带光标逐行揭示，收尾后切换完整答案与导出按钮 |
| 基准评测 | 15 HumanEval + 15 MBPP + 10 自测任务，使用官方 pytest 作为客观判据 |
| 消融实验 | G1 直接生成 / G2 +pytest 修复 / G3 +静态规则 / G4 完整链路，输出 Pass@1、可运行率、收敛率等 |
| 缺陷评测 | 20 个注入缺陷 + 20 个 clean 对照，输出规则/LLM/联合审查的 Recall 与 FPR |
| 实验持久化 | 消融与缺陷结果写入 MySQL `experiment_batches` / `experiment_runs` / `defect_metrics` |
| RAG 规范检索 | Chroma 持久集合 + 本地哈希向量 + BM25 + 查询扩展 + MMR；生成与审核阶段注入规范条款 |
| 附件上传 | 单次最多 5 个文件；支持 DOCX/XLSX/XLS/PDF/图片/文本与代码，可用于生成需求或只审核代码 |
| 文档解析与 OCR | DOCX 正文/表格/页眉页脚、Excel 工作表/原始行号/公式、PDF 文本层与扫描页 OCR、图片本地 RapidOCR |
| MD5 去重与隔离 | 按原始字节 MD5 复用附件；仅聊天与知识库用途隔离，重复文件不重复解析、不重复建立知识条目 |
| 附件可靠性 | 原件/纯文本/RAG Markdown 分离持久化；原子写入、字符预算、异常页隔离、清单失败回滚与索引失败重试 |
| 检索评估 | 20 条标注查询，对比纯向量/混合/混合+MMR 的 Recall、MRR、nDCG 与冗余度 |
| 历史管理 | 历史对话支持自定义命名（行内重命名）与删除；自动命名不覆盖手动标题 |
| 持久记忆 | 会话与消息持久化到 MySQL，新建即落库，刷新/重开浏览器不丢失 |
| 连续编号 | 消息主键按“会话→轮次→原序”重排为连续整数，删除后自动重排、无空洞 |
| 离线兜底 | 未配置 API Key 或 `FORCE_MOCK=1` 时使用内置 Mock，整条链路仍可完整运行 |

### 代码审核如何参考 EEC

代码审核模块参考开源项目 `affaan/everything-claude-code`（EEC）的方法论，落地了五个机制：

1. **新鲜上下文审核**：审核节点只接收“代码 + 需求 + 测试”，不携带生成阶段的推理，以独立视角发现盲区；
2. **确定性评分卡**：固定 5 个维度（正确性、安全性、规范与可读性、边界与异常、测试充分性），
   各维度从 10 分起按问题严重等级扣分（阻断 −4 / 主要 −2 / 次要 −1），加权得到 0–100 总分与
   A–F 等级，同样输入永远得到同样结果；
3. **质量门禁**：存在阻断问题或测试失败即门禁不通过；
4. **置信度与误报过滤**：LLM 每条问题自报 0–1 置信度，低于 0.6 的丢弃，静态工具与 pytest 发现全部保留；
5. **优先改进动作**：自动取严重等级最高的前 3 条问题，转成带行号与修复建议的可执行动作清单。

> **静态工具说明**：实际接入 Ruff（规范）、Bandit（安全）、ast（逻辑）三个确定性工具，
> 统一封装在 `services/static_analysis/` 下。

---

## 二、整体架构与工作流程

### 1. 代码生成链路

LangGraph 节点：`clarify → ask（可选中断）→ generate → review → test →（fix → review → test 循环）→ finish`，
超过最大修复轮次仍未收敛时走 `best_effort`，保留当前最优版本并列出未解决问题。

```
用户需求
  │
  ▼
clarify（需求确认，结构化 ConfirmedRequirement）
  │
  ├─ 有必须澄清的问题 ──► ask（interrupt 挂起，等待 /api/chat/resume）
  │                              │
  │                              ▼
  └────────────────────────► generate（单函数：solution.py + test_solution.py；
                                 多文件：按语言生成完整 files 项目）
                                 │
                                 ▼
review（Python 跑 Ruff/Bandit/ast；项目跑 LLM 三视角并行，合并评分）
                                 │
                                 ▼
test（Python pytest；Java javac + 运行；C gcc 编译链接 + 运行；HTML/CSS/JS 静态检查）
                                 │
          ┌──────────────────────┴──────────────────────┐
          │                                             │
    无阻断且项目检查全绿                       存在问题/检查失败
          │                                             │
          ▼                                             ▼
       finish                              fix（单文件或多文件定向修复）
                                                    │
                                                    └──► review（最多 MAX_ROUNDS 轮）
                                                          │
                                                          └─ 超轮次 ──► best_effort
```

### 2. 多语言项目生成

`services/language_detect.py` 先做确定性识别：提到 Java/C/HTML/Python、文件扩展名或“网页/系统/项目”等关键词时，
自动选定语言与交付形态；非 Python 语言与网页需求固定走多文件项目。

生成采用“能一次成功就一次生成，失败就分步生成”的两级策略：

1. **一次生成**：模型直接返回 `project_name/language/entry_file/run_command/files`；
2. **分步生成**：长 JSON 被截断或校验失败时，先让模型只输出文件计划（2~12 个路径与职责），
   再逐个文件输出纯文本内容，最后统一校验路径、入口文件、README 与文件完整性。

项目文件只剥离“包裹整个文件的外层围栏”，README 内部的 ```bash 示例代码块会完整保留，
不会因为一次正则截取而只剩一行运行命令。

生成结果统一为 `ProjectFile(path, content, language, role)`，写入 `storage/generated/chat_<run_id>/`，
按语言执行检查：

| 语言 | 检查方式 |
| --- | --- |
| Python | 有测试时运行 pytest；无测试时 `compileall` |
| Java | `javac -encoding UTF-8 -d build` 编译全部 `.java`；唯一入口且不读标准输入时用 `java -cp build` 运行 |
| C | `gcc -std=c11 -I. -o build/app` 编译链接全部 `.c`；不读标准输入时运行 `build/app` |
| HTML | 标签闭合、`<!DOCTYPE>/<html>`、本地 CSS/JS 引用存在性；JS 再跑 `node --check` |

`services/project_runner.py` 会先查 `PATH`，再查 JDK / MinGW 的常见安装目录，避免服务进程环境变量缺失时误报“工具未安装”。

### 3. 只审核链路

`review_only → test_only → finish_review`：只做静态审核与 LLM 审核；用户附了测试才运行 pytest，
不生成代码。仅审核模式允许不附测试，不会因为 `total=0` 被自动判阻断。

### 4. SSE 增量流式输出

后端以 `text/event-stream` 推送：每个 LangGraph 节点完成时立即推送一条 `stage` 事件，携带
`partial`——该时刻序列化出的“当前进度”（与最终结果同一口径，经
`ChatPipelineResult.from_state()`）。前端把已接收内容逐行揭示（末尾闪烁光标），
需求 → 代码 → 审核 → 测试各节随进度依次出现；`result` 到达且揭示追平后切换为完整答案，
并显示“导出结果 ZIP”。阶段标签在 `graph/chat_graph.py` 的 `STAGE_LABELS` 中维护。

SSE 事件共四类：

- `stage`：`{"type":"stage","node":...,"label":...,"solution_code":...,"round":...,"partial":<当前进度 payload>}`
- `interrupt`：`{"type":"interrupt","conversation_id":...,"thread_id":...,"questions":[...]}`
- `result`：`{"type":"result","conversation_id":...,"data":<完整前端 payload>}`
- `error`：`{"type":"error","message":...}`

流式中间态里版本尚未归档（test 节点才入 `versions`），前端用
`final_code/final_files/final_review/final_pytest` 拼临时版本做增量展示，
保证代码与报告随进度出现，而不是最后一次性弹出。

### 5. 三路并行审查

审核节点不再把 spec/security/logic 塞进同一次模型调用，而是拆成三个独立调用：

- `ChatLLMService.review_code_view(view, ...)`：每次只给一个视角的指令与 few-shot 示例；
- `ThreadPoolExecutor` 同时执行三个视角，默认最多 3 个 worker；
- Ruff/Bandit/ast 也通过线程池并行执行，单个工具失败只记录日志，不影响其它工具；
- 合并阶段按“行号 + 问题类型”去重，保留更严重、描述更完整的一条，避免重复扣分；
- 某个视角失败时保留其它视角结果；三个视角全部失败才回退到一次串行 `review_code`。

相关开关：`REVIEW_PARALLEL=1`（默认开启）、`REVIEW_MAX_WORKERS=4`。

### 6. 结果导出

前端结果区有“导出结果 ZIP”按钮，对应：

```http
GET /api/conversations/{conversation_id}/export?format=zip&scope=latest
GET /api/conversations/{conversation_id}/export?format=zip&scope=all
```

ZIP 内容：

- `manifest.json`：会话、导出时间、结果数量；
- `requirement.md`：原始需求 + 确认后的需求契约；
- `versions/round_XX_vYY/`：每轮 `solution.py`、`test_solution.py`、`review.md`、`pytest.json`；
- `final/`：最终代码、审核报告、pytest 结果与完整 `result.json`。

单包上限由 `EXPORT_MAX_BYTES` 控制，默认 20MB；导出只读已存结果，不重新调用模型。

### 7. 评测与消融实验

`evaluation/` 是独立评测子系统，复用主链路的模型、静态工具和 pytest 执行器：

| 组成 | 说明 |
| --- | --- |
| `datasets/humaneval15.jsonl` | 15 道官方 HumanEval，官方 `check(candidate)` 转 pytest |
| `datasets/mbpp15.jsonl` | 15 道官方 MBPP，自动推断函数名并拼接官方断言 |
| `datasets/self10.jsonl` | 10 道自建任务，覆盖空值、边界、字符串、搜索等场景 |
| `build_datasets.py` | 从官方原始文件重建固定子集，`--validate` 会逐题跑参考实现 |
| `runner.py` | G1–G4 变体运行器；G4 直接走生产 LangGraph |
| `metrics.py` | Pass@1、可运行率、测试通过率、平均修复轮次、收敛率与 CSV/Markdown |
| `defect_sets/cases20.jsonl` | 20 个注入缺陷 + 20 个 clean 对照 |
| `defect_eval.py` | 规则/LLM/联合审查的 Recall 与 FPR |
| `store.py` | MySQL 实验批次、逐题结果、缺陷指标持久化 |

开题报告要求与代码、数据、验证命令的逐条对照见
[`docs/requirements-traceability.md`](docs/requirements-traceability.md)。

分组定义：

- **G1**：单智能体直接生成，不审核、不修复；
- **G2**：生成 + pytest，失败后只按 pytest 失败信息修复；
- **G3**：生成 + Ruff/Bandit/ast + pytest，失败后按规则问题修复；
- **G4**：完整生产链路（静态 + 三视角 LLM + pytest + 修复）。

运行命令：

```powershell
# 重建并校验 40 道题（原始文件缺失时会自动下载）
python -m evaluation.build_datasets --validate

# 3 题 dry-run，先确认链路与费用
python -m evaluation.run_ablation --limit 3

# 全量 40 题 × 4 组
python -m evaluation.run_ablation

# 缺陷评测 dry-run（2 个 buggy + 2 个 clean）
python -m evaluation.run_defects --buggy-limit 2 --clean-limit 2

# 全量 20 buggy + 20 clean
python -m evaluation.run_defects
```

全量真实实验会调用 160 次生成/修复和 40 次缺陷审查，产生 API 费用与较长耗时；
仓库默认不附带全量真实结果，需在确认模型额度后手动执行。

结果写入 `storage/results/ablation_<时间>/` 与 `storage/results/defects_<时间>/`，
包括汇总 CSV、逐题 CSV、原始 JSONL 和 Markdown 报告；默认同时写入 MySQL 实验表。
`FORCE_MOCK=1` 时只验证实验骨架，结果不能作为论文数据。

指标口径：

- **Pass@1**：一道题的全部官方测试都通过记 1，否则 0；
- **可运行率**：pytest 能收集并执行至少一个用例的比例；
- **测试通过率**：所有测试用例中通过的比例（按用例加权）；
- **平均修复轮次**：进入修复循环后的平均轮数；
- **收敛率**：最终全部通过的任务比例；
- **Recall**：缺陷标签被“同行号 ±1 且同视角”的问题命中比例；
- **FPR**：clean 用例中出现阻断/主要问题的比例（纯次要风格建议不计）。

---

## 三、RAG 检索与评估

### 1. 内置知识库与上传资料

`knowledge/` 下 7 篇内置 Markdown 规范，按二级标题切分为 **41 个基线片段**：

| 文件 | 片段数 | 内容 |
| --- |---:| --- |
| `python_style.md` | 5 | 命名、注释、类型注解、代码风格 |
| `security.md` | 6 | eval/exec、反序列化、密钥、网络与异常泄露 |
| `logic_boundary.md` | 6 | 空值、越界、异常、并发、可变默认参数 |
| `testing.md` | 5 | 正常路径、边界、异常、断言有效性 |
| `project_conventions.md` | 5 | 文件命名、接口契约、最小修改原则 |
| `code_review_cases.md` | 8 | Google 工程评审规范中文化改写：设计、功能、复杂度、测试、评论粒度 |
| `security_review_cases.md` | 6 | OWASP 安全规范改写：输入校验、SQL 注入、命令/路径注入、日志与依赖 |

用户显式选择“导入知识库”的上传附件，会从 `storage/uploads/knowledge/` 追加 Markdown 文档；
仅用于当前对话的附件不会进入长期索引。因此，**41 个片段是内置知识库基线**，上传资料会额外增加片段，
不会改写上述内置文档或离线标注集。

### 2. 检索链路

```
knowledge/*.md + storage/uploads/knowledge/*.md（仅 in_knowledge=True）
  │ 按二级标题切分
  ▼
内置基线 41 个片段 + 已入库上传资料片段
  │ 本地哈希向量（512 维，中文 2/3-gram + 标识符词元）
  │ + 本地 BM25（k1=1.5, b=0.75）
  ▼
查询构造
  ├─ 生成阶段：完整需求契约（输入/输出/边界/约束/验收/假设）
  └─ 审核阶段：需求 + 代码风险信号 + 代码标识符
  ▼
规则化查询扩展（中英术语同义，最多 2 个变体）
  ▼
混合打分：0.55 向量 + 0.35 BM25 + 0.10 词元覆盖率
  ▼
MMR 重排（λ=0.85）→ Top-K
  ▼
注入生成/审核提示词，并把命中来源返回前端
```

检索层任何异常都会降级为空命中，不影响主链路继续执行。索引版本为 3，指纹覆盖片段 ID、
文档标题、小节和正文；内置或上传资料变化时自动重建 `storage/rag`。上传接口在一次批量操作后统一刷新，
刷新失败会保留待重试状态，重传 MD5 重复文件也可以再次触发刷新，不会被“已入库”状态挡住。

### 3. 检索评估

标注集在 `scripts/rag_eval_cases.json`（20 条查询，知识库 41 个片段，K=4）：

| 模式 | Recall@4 | MRR | nDCG@4 | 冗余度 |
|---|---:|---:|---:|---:|
| 纯向量 | 0.750 | 0.725 | 0.732 | 0.0215 |
| 混合检索 | 1.000 | 0.942 | 0.957 | 0.0284 |
| 混合 + MMR | 1.000 | 0.942 | 0.957 | 0.0242 |

混合检索把 Recall@4 从 0.750 提升到 1.000；MMR 在不降低 Recall/MRR/nDCG 的前提下，
把 Top-4 平均两两相似度从 0.0284 降到 0.0242。

复现：

```powershell
python -m scripts.evaluate_rag
```

逐条明细写入 `storage/rag_eval_report.json`；调研来源与逐项落地记录见
[`docs/rag-cases.md`](docs/rag-cases.md)。

---

## 四、附件上传、解析与去重

### 1. 支持格式与解析边界

| 类型 | 扩展名 | 当前实现 |
| --- | --- | --- |
| Word | `.docx` | 读取正文、表格、页眉页脚；旧版 `.doc` 返回明确的不支持提示 |
| Excel | `.xlsx`、`.xls` | 保留工作表名和原始行号；XLSX 只读读取并保留公式文本，XLS 使用 xlrd |
| PDF | `.pdf` | 优先提取文本层；有效文字过少的页面渲染后本地 OCR，单页异常不影响其他页面 |
| 图片 | `.png`、`.jpg`、`.jpeg`、`.bmp`、`.webp`、`.tif`、`.tiff` | RapidOCR 本地文字识别；只承诺 OCR，不进行通用图像语义理解 |
| 文本/代码 | `.txt`、`.py`、`.md`、`.csv`、`.json`、`.log` | UTF-8、带 BOM 的 UTF-8/UTF-16、GB18030 严格解码，保持原始代码内容 |

附件既可用于“代码生成”的需求材料，也可用于“只审核代码”。审核链路使用**纯文本**而不是带标题、
表格符号的 RAG Markdown，避免把文档标记误当成 Python 代码。OCR 引擎懒加载为单例，图片和扫描 PDF
解析在线程池中执行，普通文本上传和服务启动不会初始化 OCR，也不会阻塞 FastAPI 事件循环。
当前实现不依赖 Tesseract 或 LibreOffice。

### 2. 三类数据分离与 MD5 去重

所有附件数据都位于被 Git 忽略的运行期目录 `storage/uploads/`：

```
storage/uploads/
├── files/          # 原始附件，以附件 ID 命名，不使用用户输入路径
├── text/           # 当前对话使用的纯文本
├── knowledge/      # RAG 使用的 Markdown
└── manifest.json   # 元数据、原始文件 MD5、in_knowledge 与相对路径
```

- 附件 ID 为 `att_` 加原始文件完整字节的 32 位 MD5；MD5 只用于内容去重，并声明 `usedforsecurity=False`；
- 相同内容即使改名也复用记录，同名但内容不同会得到不同 ID；复用前还会比较原始字节，防止极端哈希冲突覆盖文件；
- 重复上传不会重复解析、OCR 或建立知识条目，无元数据变化时也不会重写 manifest；
- 上传时可选择“导入知识库”。只有 `in_knowledge=True` 的附件进入长期索引，仅聊天附件与知识资料严格隔离；
- 相同附件先用于聊天、后选择入库时，会补写 `in_knowledge` 并刷新索引；清单保存失败会回滚内存状态；
- 提取文本丢失时给出明确错误，重传相同字节可恢复；中断恢复优先读取持久化的原始需求，避免附件正文丢失。

### 3. 默认限制

| 限制项 | 默认值 |
| --- | ---: |
| 单次文件数 | 5 |
| 单文件大小 | 20 MB |
| 单附件提取文本 | 200,000 字符 |
| 对话附件 | 单文件 12,000 字符，总计 30,000 字符 |
| PDF | 最多 100 页 |
| Excel | 每个工作表最多 10,000 个非空数据行 |
| 图片 | 最多 40,000,000 像素 |
| Office 解压后总大小 | 最多 100 MB |

对话截断标记本身也计入总字符预算，保证实际注入不会超过配置上限。对应配置集中在
`config/settings.py`，可根据部署环境调整。

---

## 五、目录结构

```
Graduation Project/
├── api/
│   └── server.py              # FastAPI：REST + SSE + multipart 上传，同时托管前端静态资源
├── web/                       # 前端（HTML/CSS/JS，无需构建）
│   ├── index.html             # 单页结构（纯文字，无图片/logo）与附件上传控件
│   ├── styles.css             # 豆包式样式、左右布局、流式光标、历史项与窄屏适配
│   └── app.js                 # 会话管理、SSE 增量流式与逐行揭示、附件、富文本答案与问答卡渲染
├── graph/
│   └── chat_graph.py          # LangGraph 状态图：节点、条件边、中断恢复、阶段流式
├── models/
│   ├── common.py              # 通用枚举与 pytest 结果模型
│   └── chat_schemas.py        # 需求、审核项、评分卡、RAG 命中、版本与流水线结果（from_state 支持流式中间态）
├── prompts/
│   └── chat_prompts.py        # few-shot 提示词：需求确认/合并/生成/审核/修复
├── knowledge/                 # RAG 知识库（7 篇 Markdown，41 个片段）
│   ├── python_style.md
│   ├── security.md
│   ├── logic_boundary.md
│   ├── testing.md
│   ├── project_conventions.md
│   ├── code_review_cases.md
│   └── security_review_cases.md
├── services/
│   ├── chat_llm.py            # LLM 调用、JSON 解析、生成结果校验、重试
│   ├── chat_memory.py         # MySQL 持久记忆：会话/消息、删除重命名、连续编号
│   ├── chat_workspace.py      # 多文件工程写入、路径校验、静态工具并行调度、项目检查
│   ├── document_parser.py     # DOCX/XLSX/XLS/PDF/图片/文本解析与本地 OCR
│   ├── export_service.py      # 会话结果 ZIP 导出（需求/各轮项目 files/报告/检查结果）
│   ├── upload_service.py      # 附件 MD5 去重、原子写入、清单回滚、文本预算与知识隔离
│   ├── rag_retriever.py       # 上传知识加载、Chroma + 混合检索 + MMR、失败重试
│   ├── rag_eval.py            # Recall/MRR/nDCG/冗余度与三模式对比
│   ├── review_scorecard.py    # 确定性评分卡与低置信度过滤
│   ├── test_runner.py         # pytest 子进程执行与 JUnit 解析
│   ├── project_runner.py      # Java/C/HTML/Python 项目级编译与静态检查
│   ├── language_detect.py     # 目标语言与项目形态识别
│   ├── test_data_cleanup.py   # 只清理 test_ 前缀的测试会话与评测批次
│   └── static_analysis/       # Ruff / Bandit / ast 统一封装
├── evaluation/                # 评测与消融子系统
│   ├── datasets/              # 15 HumanEval + 15 MBPP + 10 自测任务
│   ├── defect_sets/           # 20 注入缺陷 + 20 clean 对照
│   ├── build_datasets.py      # 官方数据转固定子集并校验
│   ├── build_defect_cases.py  # 缺陷变异与标签生成
│   ├── datasets.py            # 任务加载与需求契约转换
│   ├── runner.py              # G1–G4 运行器
│   ├── metrics.py             # 指标聚合与 CSV/Markdown
│   ├── defect_eval.py         # Recall/FPR 评估
│   ├── run_ablation.py        # 消融 CLI
│   ├── run_defects.py         # 缺陷评测 CLI
│   └── store.py               # MySQL 实验表持久化
├── scripts/
│   ├── evaluate_rag.py        # RAG 评估 CLI
│   ├── cleanup_test_data.py   # 手动清理 test_ 前缀测试数据
│   ├── cleanup_rag.py         # 清理 Chroma 已废弃的 HNSW 分段目录
│   └── rag_eval_cases.json    # 20 条标注查询
├── docs/
│   ├── user-manual.md         # 项目使用手册：安装、生成/审核、附件、导出与排障
│   ├── full-chain-validation.md # 真实 API 全链路验证记录
│   ├── rag-cases.md           # GitHub RAG 案例调研与落地记录
│   └── requirements-traceability.md  # 开题报告要求与实现对照
├── tests/
│   ├── conftest.py            # 测试会话收尾：清理 test_ 前缀数据
│   ├── test_smoke.py          # 主链路、修复闭环、生成校验、测试执行器
│   ├── test_rag.py            # 检索、查询扩展、SQL 信号、评估指标
│   ├── test_interrupt.py      # 中断与恢复
│   ├── test_uploads.py        # 真实 Office/PDF、OCR 桩、MD5、清单回滚、multipart 与索引重试
│   ├── test_export.py         # ZIP 导出与接口
│   ├── test_parallel_review.py# 三路审查提示词、去重与视角强制
│   ├── test_evaluation.py     # G1–G4、指标聚合与 G4 生产图
│   └── verify_chat_loop.py    # 确定性验证“审核→测试→修复”闭环
├── storage/                   # 运行期产物（自动重建，不入库）
│   ├── generated/             # 每次运行的独立临时工程
│   ├── uploads/               # 附件原件、纯文本、RAG Markdown 与 manifest
│   ├── backups/               # 清理前的数据库与向量库备份（不提交）
│   └── rag/                   # Chroma 持久索引（自动重建）
├── requirements.txt
├── pyproject.toml
├── uv.lock
├── .env.example
└── .gitignore
```

---

## 六、持久化与连续编号机制

### 1. 新建即持久化

前端点击“新建对话”时立即调用 `POST /api/conversations`，在 `chat_conversations` 写入一行
（即使还没有任何消息）。因此浏览器刷新或重开后，空对话依然存在；若后端暂时不可用，
则先保留在本地，首次发送时仍会尝试补建。

### 2. 只有删除才会删库

会话与消息只会通过 `DELETE /api/conversations/{id}` 删除。`delete_conversation` 先删除该会话的
全部消息，再删除会话元信息，无其它删除路径。

### 3. 连续编号与自动重排

`chat_messages.id` 为自增主键。删除消息会留下编号空洞，因此删除后调用 `renumber_messages()`：

- 规范顺序：`conversation_id` → `turn_no` → 原 `id`（同轮内按真实写入先后，用户消息在前）；
- 两阶段更新避免主键冲突：先整体平移到高位区间，再依次写回 `1..N`；
- 最后执行 `ALTER TABLE ... AUTO_INCREMENT = N+1`，保证后续新增继续连续；表为空时重置为 1。

### 4. 数据表结构

- **chat_conversations**：`conversation_id` VARCHAR(64) 主键、`mode`、`title`、`created_at`、`updated_at`
- **chat_messages**：`id` BIGINT AUTO_INCREMENT 主键、`conversation_id`、`turn_no`、`role`、
  `kind`、`content`（LONGTEXT）、`created_at`，并在 `conversation_id` 上建索引
- **experiment_batches**：实验批次（`name`、`kind`、`group_list`、`tasks_count`、`notes`、`output_dir`）
- **experiment_runs**：消融逐题结果（`batch_id`、`task_id`、`group_name`、Pass@1、轮次、收敛、transcript）
- **defect_metrics**：缺陷评测汇总（`batch_id`、`mode`、TP/FN/FP/TN、`recall`、`fpr`）

---

## 七、环境与安装

- Python 3.13（`pyproject.toml` 要求 `>=3.13`）
- MySQL 8.x（本机：localhost:3306，库名 `graduation project`，账号 root；库名含空格，SQL 中需反引号）

```powershell
# 1) 进入项目根目录
cd "项目根目录"

# 2) 安装依赖（本工程使用 uv；也可用 pip）
uv pip install -r requirements.txt
# 或：pip install -r requirements.txt

# 3) 复制 .env.example 为 .env，填入大模型 API Key
#    如 MySQL 需要密码，再补一行 DB_PASSWORD=你的数据库口令
#    未配置 Key 时系统自动使用内置 Mock，链路仍可运行
```

### 大模型与离线模式

- 默认使用 **DeepSeek**（`.env` 中配置 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL=https://api.deepseek.com`，
  模型 `deepseek-chat`）；
- 也可把 `LLM_PROVIDER` 设为 `doubao` 切换到火山方舟（OpenAI 兼容接口）；
- 设置 `FORCE_MOCK=1` 可强制离线运行，Mock 使用确定性兜底代码，保证无网络也能演示完整链路。

#### 本地 Ollama 接入

系统已支持直接调用本机 Ollama，不需要 API Key。先启动 Ollama 并确认模型存在：

```powershell
ollama list          # 本机应能看到 qwen3.5:4b
ollama serve         # 若服务未启动；桌面端打开后通常会常驻
```

在 `.env` 中改为：

```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:4b
```

启动服务后访问 `/api/health`，会看到 `provider=ollama`、`mode=local`，前端状态栏显示
“本地模型 · qwen3.5:4b”。DeepSeek、火山方舟和 Mock 的原有配置不受影响。

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama 服务根地址，不要带 `/v1` |
| `OLLAMA_MODEL` | `qwen3.5:4b` | 必须与 `ollama list` 中的名称一致 |
| `OLLAMA_TIMEOUT` | `600` | 本地生成较慢，单独设置超时（秒） |
| `OLLAMA_MAX_TOKENS` | `8192` | 单次输出上限，过小会导致内容为空/截断 |
| `OLLAMA_NUM_CTX` | `16384` | 上下文窗口；提示词较长时需调大 |
| `OLLAMA_THINK` | `0` | `0` 关闭思考过程以提速，`1` 开启推理 |
| `OLLAMA_KEEP_ALIVE` | `30m` | 生成结束后模型继续驻留的时间 |
| `OLLAMA_API_KEY` | 空 | 仅远程或带鉴权的 Ollama 需要 |

本地模型只使用原生 `/api/chat` 接口（支持 `think=false` 和 `format=json`）；云端模型仍走
OpenAI 兼容接口。单卡本地部署建议再设置 `REVIEW_PARALLEL=0`，避免三路审查同时排队。

**实测口径（2026-11-15，`qwen3.5:4b`）**：简单函数需求完整走完
“需求确认→生成→审核→测试”，结果为 `COMPLETED`，5/5 个 pytest 用例通过、审核通过；
包含布尔值、无穷大等复杂边界的 `add` 需求可完整执行到“修复→保留最优版本”，但 4B 模型
自身生成的测试用例存在语法/断言错误，最终状态为 `BEST_EFFORT`。这属于小模型生成质量限制，
不是 Ollama 接口失败；可把 `OLLAMA_THINK` 设为 `1` 或改用更大的本地模型提升通过率。

### RAG 相关配置

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `RAG_COLLECTION` | `code_review_kb` | Chroma 集合名 |
| `RAG_EMBED_DIM` | `512` | 本地哈希向量维度 |
| `RAG_TOP_K` | `4` | 审核阶段注入片段数 |
| `RAG_GENERATE_TOP_K` | `3` | 生成阶段注入片段数 |
| `RAG_MIN_SCORE` | `0.08` | 最低相关度 |
| `RAG_QUERY_VARIANTS` | `2` | 查询扩展变体数 |
| `RAG_MMR_LAMBDA` | `0.85` | MMR 相关度/多样性权重 |

### 审查并发、输出上限与导出配置

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `LLM_MAX_TOKENS` | `8192` | 单次模型输出上限；长代码/长报告不再在 2048 处截断 |
| `REVIEW_PARALLEL` | `1` | 是否启用三路并行审查与静态工具并行 |
| `REVIEW_MAX_WORKERS` | `4` | 审查线程池上限 |
| `EXPORT_MAX_BYTES` | `20971520` | ZIP 导出包上限（20MB） |
| `TEST_MODE` | `0` | 设为 `1` 时会话编号带 `test_` 前缀，测试收尾只清理这类记录 |
| `EVAL_DEFAULT_LIMIT` | `40` | 评测默认任务数 |
| `EVAL_MAX_ROUNDS` | 同 `MAX_ROUNDS` | 消融实验最大修复轮次 |

### 测试数据与过期索引清理

**MySQL**：当前代码只使用 5 张表：`chat_conversations`、`chat_messages`、`experiment_batches`、
`experiment_runs`、`defect_metrics`。早期版本遗留的 LangGraph checkpoint 表（4 张）和旧
`persistence` 架构表（8 张）已确认无任何运行代码引用，已从数据库删除；4 个 smoke 评测批次
及其逐题/缺陷明细也已清空，5 张表的**表结构全部保留**，后续评测可继续写入。本次清理前的
表结构与数据备份在 `storage/backups/`（该目录被 Git 忽略）。

**测试数据隔离**：设置 `TEST_MODE=1` 启动服务后，服务端与前端自动生成的会话编号统一以 `test_` 开头；
`pytest` 结束时会通过 `tests/conftest.py` 自动调用清理，只删除 `test_` 前缀的会话及测试评测批次，
不会按时间或标题误删真实会话。也可手动执行：

```powershell
python -m scripts.cleanup_test_data
```

**向量库**：`storage/rag` 是可重建的派生索引，知识库内容变化时 RAG 会重建 Chroma 集合；
Chroma 删除集合后遗留的旧 HNSW 分段目录可用下面的脚本清理，脚本只删除
`chroma.sqlite3` 中已不再引用的 UUID 目录，不动当前活动分段与知识库原件：

```powershell
python -m scripts.cleanup_rag
```

---

## 八、运行方式

```powershell
# 启动后端（同时托管前端页面）
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000
# 浏览器访问 http://localhost:8000
```

在侧边栏可切换两种模式：

- **代码生成**：输入需求，答案随各阶段完成逐行揭示（需求确认 → 代码 → 审核 → 测试）；
  需求不明确时先弹出问答卡，回答后继续生成；失败时自动修复并追加新版本；
- **只审核代码**：粘贴代码（可附测试），流式展示审核与测试结果，不生成代码；
- **添加附件**：选择文件后可只用于当前对话，或勾选“导入知识库”；附件列表显示重复复用、解析警告和
  索引进度，发送前可移除。图片与扫描 PDF 的识别文字会作为材料，不把图片当作通用视觉问答输入。

### 接口清单

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查：模型、运行模式（`api`/`local`/`mock`）、MySQL、RAG 索引状态 |
| GET | `/api/conversations` | 读取持久化会话列表（按更新时间倒序） |
| POST | `/api/conversations` | 新建对话并立即持久化（刷新不丢失） |
| GET | `/api/conversations/{id}` | 读取单个会话的全部消息 |
| GET | `/api/conversations/{id}/export` | 导出会话结果 ZIP（`format=zip`，`scope=latest\|all`） |
| PATCH | `/api/conversations/{id}` | 重命名会话（自定义标题） |
| DELETE | `/api/conversations/{id}` | 删除会话及其消息，删除后重排消息编号 |
| POST | `/api/uploads` | multipart 附件上传；`import_to_knowledge` 控制是否进入 RAG，逐文件返回结果 |
| GET | `/api/knowledge/documents` | 列出已导入 RAG 的上传附件及元数据（不重复列出内置 Markdown） |
| POST | `/api/chat/generate` | 代码生成完整链路（SSE 流式），`attachment_ids` 可引用已上传材料 |
| POST | `/api/chat/review` | 仅审核模式（SSE 流式），支持粘贴代码与代码/文档附件 |
| POST | `/api/chat/resume` | 提交澄清回答并恢复被中断的生成链路（SSE） |
| GET | `/` | 前端首页 |

---

## 九、测试与质量

```powershell
ruff check .                    # 代码规范检查
pytest                          # 全量测试（当前 77 项；结束后自动清理 test_ 前缀数据）
python -m compileall -q api config graph models prompts services scripts evaluation
node --check web/app.js         # 前端 JavaScript 语法检查
python -m scripts.evaluate_rag  # RAG 检索评估
python -m evaluation.run_ablation --limit 3   # 消融 dry-run
python -m evaluation.run_defects --buggy-limit 2 --clean-limit 2
```

当前测试集共 **77 项**，覆盖：

- 主链路生成、仅审核、离线兜底、模型 JSON 对象校验与生成结果重试；
- 本地 Ollama 无 Key 仍在线、JSON/纯文本请求分流（`tests/test_local_provider.py`）；
- 需求中断与恢复（`tests/test_interrupt.py`）；
- pytest 失败转阻断项、修复节点拿到失败原因；
- 空测试文件清理、旧 JUnit 报告隔离；
- RAG 查询扩展、SQL 信号命中、混合检索、MMR 与三模式评估指标；
- 三路审查提示词隔离、同问题去重与模型视角强制；
- 会话结果 ZIP 导出、`latest/all` 范围与接口错误处理；
- 多语言识别、四语言离线项目、HTML 本地资源检查、路径穿越拒绝、Java/C 非交互运行、多文件 ZIP 结构；
- G1–G4 分组差异、G4 生产图闭环、指标聚合；
- 真实 DOCX/XLSX/XLS/PDF 解析、Excel 原始行号与公式保留、PDF 单页 OCR 异常隔离；
- 图片/扫描 PDF OCR 桩、文本编码、图片 OCR 无文字提示；
- 原始字节 MD5 去重、同名不同内容、重复上传不重写 manifest、清单失败回滚；
- 仅聊天/知识库隔离、损坏清单保护、缺失文本恢复、对话字符预算、multipart 上传和索引失败重试；
- `test_` 前缀测试会话/评测批次隔离与收尾清理，避免测试数据在 MySQL 中累积。

`tests/verify_chat_loop.py` 以确定性方式验证“审核 → 测试 → 修复”闭环，不依赖模型当次发挥。
OCR 单元测试使用可重复桩，真实 RapidOCR 中英文识别另行做端到端验证，避免模型初始化拖慢回归套件。

---

## 十、设计原则

1. 核心流程由 LangGraph 编排，节点职责单一、可独立测试，跨节点数据通过共享状态传递；
2. 静态工具负责确定性问题，LLM 负责逻辑/边界/异常的语义审核，pytest 负责最终可执行验证；
3. 收敛判定全部基于结构化结果（评分卡、门禁、pytest），不依赖模型主观判断；
4. 测试失败必须进入审核问题清单，修复建议必须带失败原因，避免“审核通过但测试失败”；
5. RAG 采用确定性混合检索与规则化查询扩展，不依赖在线 embedding 或 LLM 查询重写，结果可复现；
6. 敏感信息（API Key、数据库口令）只从 `.env` 读取，不在代码中硬编码；
7. 超轮次不收敛时 best-effort 输出当前最优代码与未解决清单，不挂起；
8. 附件原件、对话纯文本与 RAG Markdown 分离，MD5 仅用于内容去重，只有用户显式入库的资料才进入长期索引；
9. 前端保持豆包式简洁富文本、用户右/助手左，纯文字、无图片/logo，流式揭示与交互统一维护在 `web/` 下。



