# 基于多智能体协同的 Python 代码生成与辅助审查系统

一套以“**确定性静态工具 + 多智能体 LLM + 可执行测试 + RAG 规范检索**”为核心的 Python 代码生成与审查系统。

- **代码生成**：需求确认 → 需求不明确时中断提问 → 按需求契约生成代码与测试 → 静态工具与 LLM 联合审核
  → pytest 测试 → 未通过时定向修复，形成闭环；
- **只审核代码**：粘贴已有代码（可附测试），只做静态审核与测试，不生成、不改动用户代码；
- **附件材料**：上传 Word、Excel、PDF、图片或文本/代码附件，可用于当前对话，也可显式导入 RAG 知识库；
  原始文件按完整字节 MD5 去重，图片与扫描 PDF 使用本地 OCR 提取文字。

后端用 **FastAPI** 搭建，核心流程由 **LangGraph** 状态图编排，提示词采用 **few-shot 示例模式**，
结果以 **SSE 阶段流式**返回；会话与每轮消息持久化到 **MySQL**（新建即落库、刷新不丢失、
删除即同步删库、消息编号连续无空洞）。前端为参考豆包的简洁聊天界面，纯文字、无图片/logo，
无需 Node 构建。

---

## 一、系统能力

| 能力 | 说明 |
| --- | --- |
| 需求确认 | LLM 把口语化需求整理为结构化需求：函数名、输入输出、边界、约束、验收标准与假设 |
| 中断澄清 | 只有会改变实现结果的歧义才提问（最多 3 条）；LangGraph `interrupt()` 挂起，`/api/chat/resume` 恢复 |
| 代码生成 | 按确认需求生成实现代码，并同步生成覆盖正常/边界/异常场景的 pytest 测试 |
| 生成结果校验 | 校验 `solution_code`、`test_code` 非空且函数名与契约一致，不合格时带错误提示重试 |
| 三视角审核 | 新鲜上下文审核：规范（Ruff）、安全（Bandit）、逻辑（ast + LLM），问题定位到行、带修复建议 |
| 测试失败闭环 | pytest 失败会转成阻断级审核问题，修复节点能拿到失败用例与 traceback，不再空转 |
| 确定性评分卡 | 5 维度 0–10 量化、加权总分与 A–F 等级、质量门禁、优先改进动作；同样输入结果可复现 |
| 低置信度过滤 | LLM 问题带置信度，低于阈值自动过滤；静态工具与 pytest 的确定性结果全部保留 |
| 可执行测试 | pytest 子进程隔离执行，精确统计通过/失败并提取 traceback；每轮清理旧 JUnit 报告与旧测试文件 |
| 收敛判定 | 无阻断问题且 pytest 全绿即收敛，否则进入定向修复闭环（最多 `MAX_ROUNDS` 轮） |
| 定向修复 | 按问题清单做最小化补丁，重出版本，保留可回退版本 |
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
  └────────────────────────► generate（生成 solution.py + test_solution.py）
                                 │
                                 ▼
review（Ruff + Bandit + ast + LLM 三视角审核，产出评分卡）
                                 │
                                 ▼
test（隔离运行 pytest，把失败结果并入审核报告）
                                 │
          ┌──────────────────────┴──────────────────────┐
          │                                             │
    无阻断且 pytest 全绿                        存在问题/测试失败
          │                                             │
          ▼                                             ▼
       finish                              fix（定向修复）
                                                    │
                                                    └──► review（最多 MAX_ROUNDS 轮）
                                                          │
                                                          └─ 超轮次 ──► best_effort
```

### 2. 只审核链路

`review_only → test_only → finish_review`：只做静态审核与 LLM 审核；用户附了测试才运行 pytest，
不生成代码。仅审核模式允许不附测试，不会因为 `total=0` 被自动判阻断。

### 3. SSE 阶段流式输出

后端以 `text/event-stream` 逐阶段推送，页面加载提示随阶段实时更新。阶段标签在
`graph/chat_graph.py` 的 `STAGE_LABELS` 中维护，包括需求确认、生成、审核、测试、修复、
完成与 best-effort。

SSE 事件共四类：

- `stage`：`{"type":"stage","node":...,"label":...,"solution_code":...,"round":...}`
- `interrupt`：`{"type":"interrupt","conversation_id":...,"thread_id":...,"questions":[...]}`
- `result`：`{"type":"result","conversation_id":...,"data":<完整前端 payload>}`
- `error`：`{"type":"error","message":...}`

---

## 三、RAG 检索与评估

### 1. 内置知识库与上传资料

`knowledge/` 下 7 篇内置 Markdown 规范，按二级标题切分为 **41 个基线片段**：

| 文件 | 片段数 | 内容 |
|---|---:|---|
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
│   ├── styles.css             # 豆包式简洁样式、历史项、附件列表与窄屏适配
│   └── app.js                 # 会话管理、SSE、附件上传/移除/入库、结果/评分卡/问答卡渲染
├── graph/
│   └── chat_graph.py          # LangGraph 状态图：节点、条件边、中断恢复、阶段流式
├── models/
│   ├── common.py              # 通用枚举与 pytest 结果模型
│   └── chat_schemas.py        # 需求、审核项、评分卡、RAG 命中、版本与流水线结果
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
│   ├── chat_workspace.py      # 临时工程、静态工具调度、测试文件生命周期
│   ├── document_parser.py     # DOCX/XLSX/XLS/PDF/图片/文本解析与本地 OCR
│   ├── upload_service.py      # 附件 MD5 去重、原子写入、清单回滚、文本预算与知识隔离
│   ├── rag_retriever.py       # 上传知识加载、Chroma + 混合检索 + MMR、失败重试
│   ├── rag_eval.py            # Recall/MRR/nDCG/冗余度与三模式对比
│   ├── review_scorecard.py    # 确定性评分卡与低置信度过滤
│   ├── test_runner.py         # pytest 子进程执行与 JUnit 解析
│   └── static_analysis/       # Ruff / Bandit / ast 统一封装
├── scripts/
│   ├── evaluate_rag.py        # RAG 评估 CLI
│   └── rag_eval_cases.json    # 20 条标注查询
├── docs/
│   └── rag-cases.md           # GitHub RAG 案例调研与落地记录
├── tests/
│   ├── test_smoke.py          # 主链路、修复闭环、生成校验、测试执行器
│   ├── test_rag.py            # 检索、查询扩展、SQL 信号、评估指标
│   ├── test_interrupt.py       # 中断与恢复
│   ├── test_uploads.py        # 真实 Office/PDF、OCR 桩、MD5、清单回滚、multipart 与索引重试
│   └── verify_chat_loop.py    # 确定性验证“审核→测试→修复”闭环
├── storage/                   # 运行期产物（自动重建，不入库）
│   ├── generated/             # 每次运行的独立临时工程
│   ├── uploads/               # 附件原件、纯文本、RAG Markdown 与 manifest
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

---

## 八、运行方式

```powershell
# 启动后端（同时托管前端页面）
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000
# 浏览器访问 http://localhost:8000
```

在侧边栏可切换两种模式：

- **代码生成**：输入需求，依次展示需求确认、代码、审核问题、pytest 结果；需求不明确时先弹出问答卡，
  回答后继续生成；失败时自动修复并追加新版本；
- **只审核代码**：粘贴代码（可附测试），只展示审核与测试结果，不生成代码；
- **添加附件**：选择文件后可只用于当前对话，或勾选“导入知识库”；附件列表显示重复复用、解析警告和
  索引进度，发送前可移除。图片与扫描 PDF 的识别文字会作为材料，不把图片当作通用视觉问答输入。

### 接口清单

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查：模型、运行模式、MySQL、RAG 索引状态 |
| GET | `/api/conversations` | 读取持久化会话列表（按更新时间倒序） |
| POST | `/api/conversations` | 新建对话并立即持久化（刷新不丢失） |
| GET | `/api/conversations/{id}` | 读取单个会话的全部消息 |
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
ruff format --check .           # 格式检查
pytest                          # 全量测试（当前 50 项）
python -m compileall -q api config graph models prompts services scripts
node --check web/app.js         # 前端 JavaScript 语法检查
python -m scripts.evaluate_rag  # RAG 检索评估
```

当前测试集共 **50 项**，覆盖：

- 主链路生成、仅审核、离线兜底、模型 JSON 对象校验与生成结果重试；
- 需求中断与恢复（`tests/test_interrupt.py`）；
- pytest 失败转阻断项、修复节点拿到失败原因；
- 空测试文件清理、旧 JUnit 报告隔离；
- RAG 查询扩展、SQL 信号命中、混合检索、MMR 与三模式评估指标；
- 真实 DOCX/XLSX/XLS/PDF 解析、Excel 原始行号与公式保留、PDF 单页 OCR 异常隔离；
- 图片/扫描 PDF OCR 桩、文本编码、图片 OCR 无文字提示；
- 原始字节 MD5 去重、同名不同内容、重复上传不重写 manifest、清单失败回滚；
- 仅聊天/知识库隔离、损坏清单保护、缺失文本恢复、对话字符预算、multipart 上传和索引失败重试。

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
9. 前端保持简洁、纯文字、无图片/logo，样式与交互统一维护在 `web/` 下。
