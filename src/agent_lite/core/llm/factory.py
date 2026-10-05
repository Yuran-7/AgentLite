from __future__ import annotations

import os
from collections.abc import Callable

from agent_lite.core.config import LlmConfig
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.base import LLMProvider
from agent_lite.core.llm.openai_provider import OpenAICompatibleProvider
from agent_lite.core.llm.provider import AnthropicProvider
from agent_lite.core.llm.reasoning import with_reasoning
from agent_lite.core.llm.responses_provider import OpenAIResponsesProvider
from agent_lite.core.llm.types import LlmResponse


class DeferredProvider:
    """Let the frontend configure credentials before the first model request."""

    def __init__(self, factory: Callable[[], LLMProvider]) -> None:
        self._factory = factory

    async def chat(
        self, messages: list[dict[str, object]], tool_schemas: list[dict[str, object]],
        bus: EventBus, run_id: str, *, step: int = 0, system: str | None = None,
    ) -> LlmResponse:
        return await self._factory().chat(
            messages, tool_schemas, bus, run_id, step=step, system=system
        )


# 根据配置选择 API 协议并创建相应的 LLM Provider
def create_llm_provider(config: LlmConfig) -> LLMProvider:
    protocol = config.protocol.lower()
    generic_api_key = config.api_key or os.environ.get("LLM_API_KEY")
    base_url = config.base_url or None
    if protocol == "anthropic":
        config = with_reasoning(config)
        return AnthropicProvider(
            config.default_model,
            api_key=generic_api_key,
            base_url=base_url,
            context_window=config.context_window,
            **({"reasoning_effort": config.reasoning_effort} if config.reasoning_effort else {}),
        )
    if protocol == "openai":
        config = with_reasoning(config)
        if config.api_mode not in {"chat_completions", "responses"}:
            raise SystemExit("Config error: llm.api_mode must be chat_completions or responses")
        provider = (
            OpenAIResponsesProvider if config.api_mode == "responses" else OpenAICompatibleProvider
        )
        return provider(
            config.default_model,
            api_key=generic_api_key,
            base_url=base_url,
            context_window=config.context_window,
            **({"reasoning_effort": config.reasoning_effort} if config.reasoning_effort else {}),
        )
    raise SystemExit(
        "Config error: llm.protocol must be 'anthropic' or 'openai',"
        f" got: {config.protocol!r}"
    )
