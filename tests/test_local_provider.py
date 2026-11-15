"""本地 Ollama 接入的单元测试：不访问真实模型，只验证配置与调用分流。"""

from config import settings
from services.chat_llm import ChatLLMService


def test_ollama_endpoint_does_not_require_api_key(monkeypatch) -> None:
    """Ollama 本机接口没有 Key 也应保持在线，而不是退回 Mock。"""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "OLLAMA_API_KEY", "")
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.setattr(settings, "OLLAMA_MODEL", "qwen3.5:4b")

    api_key, base_url, model = settings.get_llm_endpoint()
    assert api_key == ""
    assert base_url == "http://127.0.0.1:11434"
    assert model == "qwen3.5:4b"

    service = ChatLLMService()
    assert service.local is True
    assert service.offline is False
    assert service._client is None  # 本地模型不创建 OpenAI 客户端


def test_local_chat_json_and_text_dispatch_to_ollama(monkeypatch) -> None:
    """JSON 与纯文本请求都应走本地分支，并复用同一套重试逻辑。"""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "OLLAMA_API_KEY", "")
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.setattr(settings, "OLLAMA_MODEL", "qwen3.5:4b")

    service = ChatLLMService()
    calls: list[bool] = []

    def fake_ollama(system: str, user: str, json_mode: bool) -> str:
        calls.append(json_mode)
        return '{"ok": true}' if json_mode else 'print("hi")'

    monkeypatch.setattr(service, "_ollama_chat", fake_ollama)

    assert service._chat_json("system", "user") == {"ok": True}
    assert service._chat_text("system", "user") == 'print("hi")'
    assert calls == [True, False]
