# 全链路运行验证报告

验证日期：2026-11-15  
项目：`基于多智能体协同的 Python 代码生成与辅助审查系统`  
验证方式：本地静态检查、全量单元测试、真实 DeepSeek API、真实浏览器交互、MySQL 持久化与导出检查。

## 一、结论

本轮未发现阻断系统运行的代码缺陷，**无需修改源码**。项目可以启动并完成“需求确认 → 生成 → 审核 → 测试 → 完成”链路；仅审核、多文件网页、会话持久化、ZIP 导出及生成网页交互均验证通过。

正式服务保持：`provider=deepseek`、`model=deepseek-chat`、`mode=api`、`memory=mysql`，RAG 为 41 个知识片段。

## 二、静态检查与单元测试

| 检查项 | 实际命令或入口 | 结果 |
| --- | --- | --- |
| Python 单元测试 | `python -m pytest -q` | 77 项通过，退出码 0 |
| 代码规范 | `python -m ruff check .` | `All checks passed!` |
| Python 语法编译 | `python -m compileall -q api config graph models prompts services scripts evaluation` | 通过 |
| 前端 JS 语法 | `node --check web/app.js` | 通过 |
| 服务健康 | `GET /api/health` | `status=ok` |
| 首页 | `GET /` | HTTP 200，4353 字节 |
| 前端 JS 资源 | `GET /static/app.js?v=16` | HTTP 200，39529 字节 |
| 前端 CSS 资源 | `GET /static/styles.css?v=16` | HTTP 200，17761 字节 |
| 主页面浏览器控制台 | 打开 `http://127.0.0.1:8000/` | 未发现控制台错误 |

## 三、真实 DeepSeek API 全链路

### 3.1 单函数生成

| 项目 | 实际结果 |
| --- | --- |
| 输入 | 编写 `square(n)`，返回 `n` 的平方并附调用示例 |
| SSE 阶段 | `clarify → generate → review → test → finish` |
| 最终状态 | `COMPLETED` |
| 生成文件 | `solution.py`、`test_solution.py` |
| 审核 | 通过 |
| pytest | 11/11 通过，`all_green=true` |
| 修复轮次 | 0 |
| 耗时 | 14.57 秒 |
| 会话落库 | 成功，包含 1 条用户消息和 1 条助手结果消息 |

### 3.2 仅审核代码

| 项目 | 实际结果 |
| --- | --- |
| 输入 | 带类型注解的 `add(a, b)` 及 1 条 pytest 用例 |
| SSE 阶段 | `review_only → test_only → finish_review` |
| 最终状态 | `COMPLETED` |
| 审核 | 通过 |
| pytest | 1/1 通过，`all_green=true` |
| 耗时 | 6.42 秒 |
| 会话落库 | 成功，2 条消息 |

### 3.3 多文件购物网页

| 项目 | 实际结果 |
| --- | --- |
| 输入 | 原生 HTML/CSS/JavaScript 静态购物网页，4 个商品、搜索、购物车数量 |
| SSE 阶段 | `clarify → generate → review → test → finish` |
| 最终状态 | `COMPLETED` |
| 生成文件 | `index.html`、`styles.css`、`app.js`、`README.md` |
| 文件非空检查 | 4 个文件均非空 |
| 静态引用 | `index.html` 正确引用 `styles.css` 与 `app.js` |
| JS 语法 | `node --check` 通过 |
| 静态检查 | 1 个页面通过，`all_green=true` |
| 审核 | 通过 |
| 修复轮次 | 0 |
| 耗时 | 15.08 秒 |
| 会话落库 | 成功，2 条消息 |

### 3.4 生成网页的真实浏览器交互

将上述购物网页临时托管到 8002 端口后进行验证：

1. 页面标题为“静态购物网页”，苹果、香蕉、橙子、葡萄 4 个商品正常显示；
2. 初始购物车数量为 0，点击“加入购物车”后变为 1；
3. 搜索“不存在的商品”后，商品按钮从 4 个变为 0 个，并显示“没有找到匹配的商品”；
4. 验证结束后已停止 8002 临时服务。

### 3.5 会话 ZIP 导出

对单函数会话执行 `GET /api/conversations/test_deepseek_function_001/export?format=zip&scope=latest`：

- 下载成功，ZIP 完整性检查无损坏文件；
- 共 14 个条目；
- 包含 `manifest.json`、`requirement.md`、`result.json`、轮次版本、`final/files/`、`final/review.md`、`final/pytest.json` 等。

## 四、澄清中断说明

分别使用排序函数和 `calculate(a, b, op)` 需求进行真实 API 验证。两次均由模型自行补齐合理默认并直接完成，因此未触发中断：

- 排序需求：`COMPLETED`，pytest 12/12 通过；
- 四则运算需求：`COMPLETED`，pytest 16/16 通过。

“模型选择提问”不是每次都必须发生的行为。中断挂起与 `/api/chat/resume` 恢复路径由现有 `tests/test_interrupt.py` 及全量 77 项测试覆盖。

## 五、测试数据清理

真实 API 验证完成后：

- 删除 5 个 `test_` 会话和 10 条消息；
- 删除对应的 5 个测试生成目录；
- 会话列表中剩余 `test_` 会话数为 0；
- 8001、8002 临时端口均未再监听；
- 清理后再次运行全量 pytest，77 项通过；
- 正式 8000 服务仍为 DeepSeek，RAG 仍为 41 个知识片段。

## 六、本轮未覆盖范围

- 未执行真实 40 题 × G1–G4 共 160 次的完整消融实验；该实验耗时长且会消耗较多 API 额度。
- 未对所有云端/本地模型逐一做全链路测试；本轮真实 API 使用默认 DeepSeek。
- 未对全部附件类型重新执行真实 OCR；附件与 OCR 的确定性单元测试包含在 77 项测试中。
- 小模型生成质量不属于接口可用性的充分条件；复杂需求仍可能因模型自身代码/测试质量进入 `BEST_EFFORT`。
