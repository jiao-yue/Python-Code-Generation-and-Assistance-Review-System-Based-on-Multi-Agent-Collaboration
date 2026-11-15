# 本地 Ollama 接入说明

## 配置

1. 安装并启动 Ollama，确认 `ollama list` 中已有目标模型（实测为 `qwen3.5:4b`）。
2. 在 `.env` 中设置：

```dotenv
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:4b
OLLAMA_TIMEOUT=600
OLLAMA_MAX_TOKENS=8192
OLLAMA_NUM_CTX=16384
OLLAMA_THINK=0
OLLAMA_KEEP_ALIVE=30m
```

本地调用不需要 API Key。`OLLAMA_BASE_URL` 填服务根地址即可；即使误填 `/v1`，代码也会自动去掉该后缀。

## 实现口径

- `LLM_PROVIDER=ollama` 时，`services/chat_llm.py` 走 Ollama 原生 `POST /api/chat`；
- JSON 请求设置 `format=json`，并传 `think`、`keep_alive`、`num_predict`、`num_ctx`；
- DeepSeek、火山方舟仍走 OpenAI 兼容接口；`FORCE_MOCK=1` 和云端无 Key 的离线 Mock 行为不变；
- 本地模型独立使用 `OLLAMA_TIMEOUT` 与 `OLLAMA_MAX_TOKENS`，不沿用云端较短的超时；
- 单卡本地部署建议 `REVIEW_PARALLEL=0`，避免三视角审查同时排队。

## 实测

- `qwen3.5:4b` 简单函数需求完整走完“需求确认→生成→审核→测试”，结果 `COMPLETED`，pytest 5/5 通过、审核通过；
- 含布尔值、无穷大等复杂边界的 `add` 需求可完整执行到“修复→保留最优版本”，但最终为 `BEST_EFFORT`，原因是 4B 模型生成的测试代码存在语法/断言错误，并非 Ollama 接口失败；
- 如需提高复杂用例通过率，可设置 `OLLAMA_THINK=1` 或换用更大的本地模型。

## 验证

```powershell
pytest tests/test_local_provider.py -q
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000
```

`tests/test_local_provider.py` 覆盖“无 Key 仍在线”以及 JSON/纯文本都分流到 Ollama。
