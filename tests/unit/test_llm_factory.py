from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_lite.core.config import LlmConfig
from agent_lite.core.llm import factory


# 功能：验证工厂按 openai 协议选择 OpenAI-compatible Provider 并传递配置
# 设计：替换构造器为 MagicMock，避免真实 SDK 初始化并精确检查模型、通用密钥和 base URL
def test_factory_selects_openai_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(factory, "OpenAICompatibleProvider", constructor)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    config = LlmConfig(
        protocol="openai",
        default_model="deepseek-v3",
        base_url="https://openai.example.com/v1",
    )

    result = factory.create_llm_provider(config)

    assert result is constructor.return_value
    constructor.assert_called_once_with(
        "deepseek-v3",
        api_key="test-key",
        base_url="https://openai.example.com/v1",
    )


# 功能：验证工厂按 anthropic 协议选择 Anthropic Provider 并保留空 base URL
# 设计：不设置通用密钥且注入假构造器，确认标准协议密钥的回退逻辑留给具体 Provider
def test_factory_selects_anthropic_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(factory, "AnthropicProvider", constructor)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    config = LlmConfig(protocol="anthropic", default_model="deepseek-chat")

    result = factory.create_llm_provider(config)

    assert result is constructor.return_value
    constructor.assert_called_once_with(
        "deepseek-chat",
        api_key=None,
        base_url=None,
    )


# 功能：验证直接构造非法 LlmConfig 时工厂仍会拒绝未知协议
# 设计：绕过 TOML 和环境校验传入非法值，覆盖工厂作为最后一道防线的分支
def test_factory_rejects_unknown_protocol() -> None:
    config = LlmConfig(protocol="unknown")

    with pytest.raises(SystemExit, match="llm.protocol"):
        factory.create_llm_provider(config)
