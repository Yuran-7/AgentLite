from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_lite.core.app import CoreApp
from agent_lite.core.bus.commands import SessionReasoningCommand
from agent_lite.core.bus.envelope import HandlerError
from agent_lite.core.config import AgentLiteConfig, LlmConfig
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.factory import create_llm_provider
from agent_lite.core.llm.openai_provider import OpenAICompatibleProvider
from agent_lite.core.llm.reasoning import supported_efforts
from agent_lite.core.llm.responses_provider import OpenAIResponsesProvider
from agent_lite.core.llm.settings import model_settings, resolve_model
from agent_lite.core.session.manager import SessionManager
from agent_lite.core.session.model import Session
from agent_lite.core.session.store import SessionStore
from tests.unit.test_llm_provider import FakeStream, _make_final
from tests.unit.test_model_settings import write_settings
from tests.unit.test_openai_provider import FakeOpenAIStream, _text_chunk


# 功能：两种 OpenAI 接口发送各自的推理参数，默认请求不增加参数。
# 设计：通过注入客户端截获最终 kwargs，验证实际传输而非只检查配置。
@pytest.mark.parametrize("responses", [False, True])
@pytest.mark.parametrize("effort", ["", "high", "xhigh"])
async def test_request_effort(responses, effort):
    client = MagicMock()
    response = NS(type="response.completed", response=NS(status="completed", output=[], usage=None))
    stream = FakeOpenAIStream([response] if responses else [_text_chunk("done", "stop")])
    stream.close = AsyncMock()
    create = AsyncMock(return_value=stream)
    if responses:
        client.responses.create = create
    else:
        client.chat.completions.create = create
    cls = OpenAIResponsesProvider if responses else OpenAICompatibleProvider
    await cls("test", client=client, reasoning_effort=effort).chat([], [], EventBus(), "test")
    kwargs = create.await_args.kwargs
    if responses:
        assert kwargs.get("reasoning") == ({"effort": effort} if effort else None)
        assert "reasoning_effort" not in kwargs
    else:
        assert kwargs.get("reasoning_effort") == (effort or None)
        assert "reasoning" not in kwargs


# 功能：推理设置可读写、重置、持久化，忙碌时拒绝变更。
# 设计：使用真实 SessionManager 和存储模拟重启，验证会话隔离与互斥。
async def test_session_reasoning_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = AgentLiteConfig()
    config.llm = LlmConfig(protocol="openai", default_model="gpt-6.1-sol", reasoning_effort="medium")
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: None, EventBus())
    session = await manager.create("chat", workspace_root=str(tmp_path))
    session.run_ids.append("run")
    store.append_message(session.id, "user", "hello")
    app = CoreApp()
    app._config, app._sessions = config, manager
    params = {"session_id": session.id}
    result = await app._session_reasoning_handler(params)
    assert result["effort"] == "" and result["effective_effort"] == "medium"
    assert result["efforts"] == ("low", "medium", "high", "xhigh", "max")
    await app._session_reasoning_handler({**params, "effort": "xhigh"})
    other = SessionManager(store, lambda: None, EventBus())
    resumed = await other.resume(session.id, str(tmp_path))
    assert resumed.reasoning_effort == "xhigh"
    assert (await app._session_resume_handler(params)).reasoning_effort == "xhigh"
    async with manager._locks[session.id]:
        with pytest.raises(HandlerError, match="busy"):
            await app._session_reasoning_handler({**params, "effort": "low"})
    with pytest.raises(HandlerError, match="不支持"):
        await app._session_reasoning_handler({**params, "effort": "none"})
    assert session.reasoning_effort == "xhigh"
    await app._session_reasoning_handler({**params, "effort": ""})
    assert store.read_meta(session.id).reasoning_effort == ""
    config.llm.protocol = "anthropic"
    assert not (await app._session_reasoning_handler(params))["supported"]
    with pytest.raises(HandlerError, match="不支持"):
        await app._session_reasoning_handler({**params, "effort": "high"})


# 功能：模型配置默认推理强度生效，非法值及协议组合被拒绝。
# 设计：验证 settings 到 Provider 的完整传递链，并覆盖旧会话没有字段的兼容。
def test_profile_effort_and_legacy_session(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("EFFORT_TEST_KEY", "test")
    profile = {"id": "test", "model": "gpt-6.1-sol", "protocol": "openai",
               "apiKeyEnv": "EFFORT_TEST_KEY", "reasoningEffort": "high"}
    write_settings(tmp_path, {"models": [profile]})
    resolved = resolve_model(LlmConfig(), "test", None)
    assert create_llm_provider(resolved)._reasoning_effort == "high"
    for invalid in ["invalid", 4, None]:
        write_settings(tmp_path, {"models": [{**profile, "reasoningEffort": invalid}]})
        with pytest.raises(ValueError):
            model_settings()
    write_settings(tmp_path, {"models": [{**profile, "protocol": "anthropic"}]})
    with pytest.raises(ValueError, match="reasoningEffort"):
        model_settings()
    old = {"id": "old", "mode": "chat", "status": "active", "created_at": "now", "updated_at": "now"}
    assert Session.from_dict(old).reasoning_effort == ""
    assert SessionReasoningCommand(session_id="test", effort="max").effort == "max"
    assert "none" not in supported_efforts("gpt-6.1-sol")


# 功能：Opus 的五档强度通过 Anthropic 参数发送，旧 Claude 不添加参数。
# 设计：截获实际流式调用验证请求格式，并检查工厂和配置的完整传递链。
@pytest.mark.parametrize("effort", ["", "low", "medium", "high", "xhigh", "max"])
async def test_anthropic_effort_request(effort):
    client = MagicMock()
    client.messages.stream.return_value = FakeStream(["done"], _make_final())
    config = LlmConfig(protocol="anthropic", default_model="claude-opus-5-5",
                       api_key="test", reasoning_effort=effort)
    provider = create_llm_provider(config)
    provider._client = client
    await provider.chat([], [], EventBus(), "test")
    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs.get("output_config") == ({"effort": effort} if effort else None)
    assert "reasoning_effort" not in kwargs and "reasoning" not in kwargs
    assert "thinking" not in kwargs


# 功能：方舟模型的合法档位经过配置校验并传入 Messages 请求。
# 设计：覆盖四种真实模型配置和实际 SDK 参数，验证 UI 解锁后的请求链。
@pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash", "kimi-k3", "deepseek-v4.1-flash"])
@pytest.mark.parametrize("effort", ["low", "high", "max"])
async def test_ark_effort_request(tmp_path, monkeypatch, model, effort):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("ARK_TEST_KEY", "test")
    write_settings(tmp_path, {"models": [{"id": "ark", "model": model,
        "protocol": "anthropic", "apiKeyEnv": "ARK_TEST_KEY", "reasoningEffort": effort}]})
    config = resolve_model(LlmConfig(), "ark", None)
    client = MagicMock()
    client.messages.stream.return_value = FakeStream(["done"], _make_final())
    provider = create_llm_provider(config)
    provider._client = client
    await provider.chat([], [], EventBus(), "test")
    assert client.messages.stream.call_args.kwargs["output_config"] == {"effort": effort}
    assert "medium" not in supported_efforts(model, "anthropic")


# 功能：Opus 支持读取、保存和恢复 Effort 配置，不支持的档位被拒绝。
# 设计：通过真实会话处理器和模型配置解析验证后端校验与元数据一致。
async def test_opus_session_effort(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("OPUS_TEST_KEY", "test")
    write_settings(tmp_path, {"models": [{"id": "opus", "model": "claude-opus-5-5",
        "protocol": "anthropic", "apiKeyEnv": "OPUS_TEST_KEY", "reasoningEffort": "medium"}]})
    manager = SessionManager(SessionStore(tmp_path / "sessions"), lambda: None, EventBus())
    session = await manager.create("chat")
    app = CoreApp()
    app._config, app._sessions = AgentLiteConfig(), manager
    await app._session_model_handler({"session_id": session.id, "model_id": "opus"})
    result = await app._session_reasoning_handler({"session_id": session.id})
    assert result["supported"] and result["effective_effort"] == "medium"
    assert result["efforts"] == ("low", "medium", "high", "xhigh", "max")
    result = await app._session_reasoning_handler({"session_id": session.id, "effort": "xhigh"})
    assert result["effective_effort"] == "xhigh" and session.reasoning_effort == "xhigh"
    with pytest.raises(HandlerError, match="不支持"):
        await app._session_reasoning_handler({"session_id": session.id, "effort": "none"})
    assert supported_efforts("claude-haiku-4-5", "anthropic") == ()


# 功能：切换模型保留兼容的推理选择，切换到 Anthropic 清除覆盖。
# 设计：使用真实模型配置解析和会话管理，避免 UI 仅改变显示但后端仍携带旧参数。
async def test_model_switch_clears_incompatible_reasoning(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SWITCH_TEST_KEY", "test")
    write_settings(tmp_path, {"models": [
        {"id": "openai", "model": "gpt-6.1-sol", "protocol": "openai", "apiKeyEnv": "SWITCH_TEST_KEY"},
        {"id": "claude", "model": "test", "protocol": "anthropic", "apiKeyEnv": "SWITCH_TEST_KEY"},
    ]})
    manager = SessionManager(SessionStore(tmp_path / "sessions"), lambda: None, EventBus())
    session = await manager.create("chat")
    session.reasoning_effort = "high"
    app = CoreApp()
    app._config, app._sessions = AgentLiteConfig(), manager
    result = await app._session_model_handler({"session_id": session.id, "model_id": "openai"})
    assert result["reasoning_effort"] == "high"
    result = await app._session_model_handler({"session_id": session.id, "model_id": "claude"})
    assert result["reasoning_effort"] == ""
