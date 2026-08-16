from __future__ import annotations

import os

from agent_lite.core.config import LlmConfig
from agent_lite.core.llm.base import LLMProvider
from agent_lite.core.llm.openai_provider import OpenAICompatibleProvider
from agent_lite.core.llm.provider import AnthropicProvider


# 根据配置选择 API 协议并创建相应的 LLM Provider
def create_llm_provider(config: LlmConfig) -> LLMProvider:
    protocol = config.protocol.lower()
    generic_api_key = os.environ.get("LLM_API_KEY")
    base_url = config.base_url or None
    if protocol == "anthropic":
        return AnthropicProvider(
            config.default_model,
            api_key=generic_api_key,
            base_url=base_url,
        )
    if protocol == "openai":
        return OpenAICompatibleProvider(
            config.default_model,
            api_key=generic_api_key,
            base_url=base_url,
        )
    raise SystemExit(
        "Config error: llm.protocol must be 'anthropic' or 'openai',"
        f" got: {config.protocol!r}"
    )
