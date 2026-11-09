# 基于多智能体协同的 Python 代码生成与辅助审查系统

一套以“**确定性静态工具 + 多智能体 LLM + 可执行测试**”为核心的 Python 代码生成与审查系统。
系统提供两种使用方式：

- **代码生成**：用户给需求 → LLM 需求确认 → 按确认需求生成代码与测试 → 静态工具与 LLM 联合审核
  → pytest 测试 → 未通过时定向修复，形成闭环；
- **只审核代码**：粘贴已有代码（可附测试），只做静态审核与测试，不生成、不改动用户代码。

后端用 **FastAPI** 搭建，核心流程由 **LangGraph** 状态图编排，提示词采用 **few-shot 示例模式**，
结果以 **SSE 阶段流式**返回；会话与每轮消息持久化到 **MySQL**（新建即落库、刷新不丢失、
删除即同步删库、消息编号连续无空洞）。前端为参考豆包的简洁聊天界面，纯文字、无图片/logo，
无需 Node 构建。

---

## 一、系统能力

| 能力 | 说明 |
| --- | --- |
| 需求确认 | LLM 把口语化需求整理为结构化需求：函数名、输入输出、边界、约束、验收标准与假设 |
| 代码生成 | 按确认需求生成实现代码，并同步生成覆盖正常/边界场景的 pytest 测试 |
| 三视角审核 | 新鲜上下文审核：规范（Ruff）、安全（Bandit）、逻辑（ast + LLM），问题定位到行、带修复建议 |
| 确定性评分卡 | 5 维度 0–10 量化、加权总分与 A–F 等级、质量门禁、优先改进动作；同样输入结果可复现 |
| 低置信度过滤 | 每条 LLM 问题带置信度，低于阈值自动过滤以降低误报；静态工具发现全部保留 |
| 可执行测试 | pytest 子进程隔离执行，精确统计通过/失败并提取 traceback |
| 收敛判定 | 无阻断问题且 pytest 全绿即收敛，否则进入定向修复闭环（最多 `MAX_ROUNDS` 轮） |
| 定向修复 | 按问题清单做最小化补丁，重出版本，保留可回退版本 |
| 历史管理 | 历史对话支持自定义命名（行内重命名）与删除；自动命名不覆盖手动标题 |
| 持久记忆 | 会话与消息持久化到 MySQL，新建即落库，刷新/重开浏览器不丢失 |
| 连续编号 | 消息主键按“会话→轮次→原序”重排为连续整数，删除后自动重排、无空洞 |
| 离线兜底 | 未配置 API Key 或 `FORCE_MOCK=1` 时使用内置 Mock，整条链路仍可完整运行 |

### 代码审核如何参考 EEC

代码审核模块参考开源项目 `affaan/everything-claude-code`（EEC）的方法论，落地了五个机制：

1. **新鲜上下文审核**：审核节点只接收“代码 + 需求”，不携带生成阶段的推理，以独立视角发现盲区；
2. **确定性评分卡**：固定 5 个维度（正确性、安全性、规范与可读性、边界与异常、测试充分性），
   各维度从 10 分起按问题严重等级扣分（阻断 −4 / 主要 −2 / 次要 −1），加权得到 0–100 总分与
   A–F 等级，同样输入永远得到同样结果；
3. **质量门禁**：存在阻断问题或测试失败即门禁不通过；
4. **置信度与误报过滤**：LLM 每条问题自报 0–1 置信度，低于 0.6 的丢弃，静态工具发现（确定性）全部保留；
5. **优先改进动作**：自动取严重等级最高的前 3 条问题，转成带行号与修复建议的可执行动作清单。

> **静态工具说明**：实际接入 Ruff（规范）、Bandit（安全）、ast（逻辑）三个确定性工具，
> 统一封装在 `services/static_analysis/` 下。

---

## 二、整体架构与工作流程

### 1. 代码生成链路

LangGraph 节点：`clarify → generate → review → test →（fix → review → test 循环）→ finish`，
超过最大修复轮次仍未收敛时走 `best_effort`，保留当前最优版本并列出未解决问题。

```
用户需求
  │
  ▼
clarify（需求确认，结构化 ConfirmedRequirement）
  │
  ▼
generate（生成 solution.py + test_solution.py）
  │
  ▼
review（Ruff + Bandit + ast 静态工具 + LLM 三视角审核，产出评分卡）
  │
  ▼
test（隔离运行 pytest）
  │
  ├─ 无阻断且测试全绿 ──► finish（收敛，返回最终结果）
  │
  └─ 存在问题/测试失败 ──► fix（定向修复）──► 回到 review（最多 MAX_ROUNDS 轮）
                           超轮次 ──► best_effort
```

### 2. 只审核链路

`review_only → test_only → finish_review`：只做静态审核与 LLM 审核，有测试则运行 pytest，
不生成代码。

### 3. SSE 阶段流式输出

前端提交后，后端以 `text/event-stream` 逐阶段推送，页面上的加载提示随阶段实时更新。
阶段标签（`graph/chat_graph.py` 的 `STAGE_LABELS`）：

| 节点 | 页面提示 |
| --- | --- |
| clarify | 正在确认需求… |
| generate | 正在生成代码… |
| review / review_only | 正在审核代码… |
| test / test_only | 正在运行测试… |
| fix | 正在修复代码… |
| finish / finish_review | 处理完成 |
| best_effort | 已达最大修复轮次，保留当前最优版本 |

SSE 事件以 `data:` 行承载、事件间以空行分隔，共三类：

- `stage`：`{"type":"stage","node":...,"label":...,"solution_code":...,"round":...}`
- `result`：`{"type":"result","conversation_id":...,"data":<完整前端 payload>}`
- `error`：`{"type":"error","message":...}`

---

## 三、目录结构

```
Graduation Project/
├── api/
│   └── server.py              # FastAPI 后端：REST + SSE 接口，同时托管前端静态资源
├── web/                       # 前端（HTML/CSS/JS，无需构建）
│   ├── index.html             # 单页结构（纯文字，无图片/logo）
│   ├── styles.css             # 豆包式简洁样式、历史项与重命名/删除按钮
│   └── app.js                 # 会话管理、SSE 接收、结果/评分卡渲染
├── graph/
│   └── chat_graph.py          # LangGraph 状态图：节点、条件边、阶段流式与收敛判定
├── models/
│   ├── common.py              # 通用枚举（严重等级、视角等）
│   └── chat_schemas.py        # Pydantic 模型：需求确认、审核项、评分卡、版本、流水线结果
├── prompts/
│   └── chat_prompts.py        # few-shot 示例化提示词：需求确认/生成/审核/修复
├── services/
│   ├── chat_llm.py            # LLM 调用：OpenAI 兼容接口、JSON 解析、重试与离线 Mock
│   ├── chat_memory.py         # MySQL 持久记忆：会话/消息读写、删除重命名、连续编号重排
│   ├── chat_workspace.py      # 临时工程管理、静态工具调度
│   ├── review_scorecard.py    # 确定性评分卡与低置信度过滤
│   ├── test_runner.py         # pytest 子进程执行与结果解析
│   └── static_analysis/       # 确定性静态工具
│       ├── base_runner.py     # 统一子进程运行器
│       ├── ruff_runner.py     # 规范（Ruff）
│       ├── bandit_runner.py   # 安全（Bandit）
│       ├── ast_runner.py      # 逻辑（ast 语法树）
│       └── severity_mapper.py # 工具问题 → 统一严重等级
├── config/
│   └── settings.py            # 路径、大模型、数据库、收敛阈值等集中配置
├── tests/
│   ├── test_smoke.py          # 冒烟测试
│   └── verify_chat_loop.py    # 确定性验证“审核→测试→修复”闭环
├── storage/                   # 运行期产物（自动重建，不入库）
│   └── generated/             # 每次运行的独立临时工程（solution.py、test_solution.py）
├── requirements.txt           # 依赖清单
├── pyproject.toml             # uv / ruff 等工具配置
├── uv.lock                    # uv 锁定的依赖版本
├── .env.example               # 环境变量模板（真实 .env 不提交）
└── .gitignore
```

---

## 四、持久化与连续编号机制

### 1. 新建即持久化

前端点击“新建对话”时，`newConversation()` 会立即调用 `POST /api/conversations` 在
`chat_conversations` 写入一行（即使还没有任何消息）。因此浏览器刷新或重开后，空对话依然存在；
若后端暂时不可用，则先保留在本地，首次发送时仍会尝试补建。

### 2. 只有删除才会删库

会话与消息只会通过 `DELETE /api/conversations/{id}` 删除。`delete_conversation` 先删除该会话的
全部消息，再删除会话元信息，无其它删除路径。

### 3. 连续编号与自动重排

`chat_messages.id` 为自增主键。删除消息会留下编号空洞，因此删除后调用 `renumber_messages()`：

- 规范顺序：`conversation_id`（会话）→ `turn_no`（轮次）→ 原 `id`（同轮内真实写入先后，
  用户消息在前）；
- 两阶段更新避免主键冲突：先把所有 id 平移到高位区间，再依次写回 `1..N`；
- 最后执行 `ALTER TABLE ... AUTO_INCREMENT = N+1`，保证后续新增继续连续；表为空时把自增计数器重置为 1。

消息表无外键约束，重排主键安全；会话内的轮次语义由 `turn_no` 独立表达。

### 4. 数据表结构

- **chat_conversations**：`conversation_id` VARCHAR(64) 主键、`mode`、`title`、`created_at`、`updated_at`
- **chat_messages**：`id` BIGINT AUTO_INCREMENT 主键、`conversation_id`、`turn_no`、`role`、
  `kind`、`content`（LONGTEXT）、`created_at`，并在 `conversation_id` 上建索引

---

## 五、环境与安装

- Python 3.10+（本工程实际使用 3.13）
- MySQL 8.x（本机：localhost:3306，库名 `graduation project`，账号 root；库名含空格，SQL 中需反引号）

```powershell
# 1) 进入项目根目录
cd "项目根目录"

# 2) 安装依赖（本工程使用 uv；也可用 pip）
uv pip install -r requirements.txt
# 或：pip install -r requirements.txt

# 3) 复制 .env.example 为 .env，填入大模型 API Key
#    未配置 Key 时系统自动使用内置 Mock，链路仍可运行
```

### 大模型与离线模式

- 默认使用 **DeepSeek**（`.env` 中配置 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL=https://api.deepseek.com`，
  模型 `deepseek-chat`）；
- 也可把 `LLM_PROVIDER` 设为 `doubao` 切换到火山方舟（OpenAI 兼容接口）；
- 设置 `FORCE_MOCK=1` 可强制离线运行，Mock 刻意使用带缺陷的初始代码，用于演示“审核→修复”闭环。

---

## 六、运行方式

```powershell
# 启动后端（同时托管前端页面）
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000
# 浏览器访问 http://localhost:8000
```

在侧边栏可切换两种模式：

- **代码生成**：输入需求，依次展示需求确认、代码、审核问题、pytest 结果，失败时自动修复并追加新版本；
- **只审核代码**：粘贴代码（可附测试），只展示审核与测试结果，不生成代码。

### 接口清单

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查：模型提供方、模型名、运行模式（api/mock）、记忆状态 |
| GET | `/api/conversations` | 读取持久化会话列表（按更新时间倒序） |
| POST | `/api/conversations` | 新建对话并立即持久化（刷新不丢失） |
| GET | `/api/conversations/{id}` | 读取单个会话的全部消息 |
| PATCH | `/api/conversations/{id}` | 重命名会话（自定义标题） |
| DELETE | `/api/conversations/{id}` | 删除会话及其消息，删除后重排消息编号 |
| POST | `/api/chat/generate` | 代码生成完整链路（SSE 流式） |
| POST | `/api/chat/review` | 仅审核模式（SSE 流式） |
| GET | `/` | 前端首页 |

---

## 七、测试与质量

```powershell
ruff check .                  # 代码规范检查
ruff format --check .         # 格式检查
pytest                        # 冒烟测试 + 闭环验证
```

`tests/verify_chat_loop.py` 以确定性方式验证“审核 → 测试 → 修复”闭环，不依赖模型当次发挥。

---

## 八、设计原则

1. 核心流程由 LangGraph 编排，节点职责单一、可独立测试，跨节点数据通过共享状态传递；
2. 静态工具负责确定性问题，LLM 负责逻辑/边界/异常的语义审核，两者互补；
3. 收敛判定全部基于结构化结果（评分卡、门禁、pytest），不依赖模型主观判断；
4. 敏感信息（API Key、数据库口令）只从 `.env` 读取，不在代码中硬编码；
5. 超轮次不收敛时 best-effort 输出当前最优代码与未解决清单，不挂起；
6. 前端保持简洁、纯文字、无图片/logo，样式与交互统一维护在 `web/` 下。
