from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import anthropic
import httpx

from agent_lite.core.bus.events import (
    LlmModelSelectedEvent,
    LlmThinkingEvent,
    LlmTokenEvent,
    LlmUsageEvent,
)
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.assets import expand_assets
from agent_lite.core.llm.thinking import block_dict
from agent_lite.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-opus-4-7": 200_000,
}

_MAX_STREAM_RETRIES = 3
_RETRY_BACKOFF_S = (1.0, 2.0, 4.0)

log = logging.getLogger(__name__)


# 返回指定模型的最大 context window token 数
def _context_window(model: str) -> int:
    return _MODEL_CONTEXT_WINDOWS.get(model, 200_000)


_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Use the available tools to complete the user's goal. "
    "When the goal is fully achieved, respond with a final answer and do not call any more tools."
)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class AnthropicProvider:
    # 初始化 Anthropic 客户端；client 可在测试时注入以跳过 API key 检查
    def __init__(
        self,
        model: str,
        client: Any = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        context_window: int | None = None,
        reasoning_effort: str = "",
    ) -> None:
        if client is None:
            resolved_api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not resolved_api_key:
                raise SystemExit("ANTHROPIC_API_KEY not set")
            if base_url:
                self._client: Any = anthropic.AsyncAnthropic(
                    api_key=resolved_api_key,
                    base_url=base_url,
                )
            else:
                self._client = anthropic.AsyncAnthropic(api_key=resolved_api_key)
        else:
            self._client = client
        self._model = model
        self._reasoning_effort = reasoning_effort
        if context_window is not None and (type(context_window) is not int or context_window <= 0):
            raise ValueError("context_window must be a positive integer")
        self._context_window = context_window or _context_window(model)
        self._context_window_estimated = context_window is None

    # 流式调用 Anthropic API，逐 token 发布事件并返回 LlmResponse；网络中断时自动重试
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        await bus.publish(
            LlmModelSelectedEvent(run_id=run_id, model=self._model, strategy="static", ts=_now())
        )

        system_blocks: list[dict[str, object]] = [
            {
                "type": "text",
                "text": system or _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
        ]

        tools: list[dict[str, object]] = list(tool_schemas)
        if tools:
            last = dict(tools[-1])
            last["cache_control"] = {"type": "ephemeral"}
            tools = tools[:-1] + [last]

        kwargs: dict[str, object] = {
            "model": self._model,
            "max_tokens": 8192,
            "system": system_blocks,
            "messages": expand_assets(messages),
        }
        if tools:
            kwargs["tools"] = tools
        if self._reasoning_effort:
            kwargs["output_config"] = {"effort": self._reasoning_effort}

        text_parts: list[str] = []
        final_message: Any = None
        published_text = False
        published_thinking = False

        for attempt in range(1, _MAX_STREAM_RETRIES + 1):
            text_parts = []
            if published_text:
                await bus.publish(LlmTokenEvent(run_id=run_id, token="", reset=True, ts=_now()))
                published_text = False
            if published_thinking:
                await bus.publish(LlmThinkingEvent(run_id=run_id, reset=True, ts=_now()))
                published_thinking = False
            try:
                async with self._client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        if event.type != "content_block_delta":
                            continue
                        delta = event.delta
                        if delta.type == "text_delta":
                            text = delta.text
                            await bus.publish(LlmTokenEvent(run_id=run_id, token=text, ts=_now()))
                            published_text = True
                            text_parts.append(text)
                        elif delta.type == "thinking_delta":
                            await bus.publish(
                                LlmThinkingEvent(run_id=run_id, token=delta.thinking, ts=_now())
                            )
                            published_thinking = True
                    final_message = await stream.get_final_message()
                break  # success
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as exc:
                if attempt == _MAX_STREAM_RETRIES:
                    log.error(
                        "stream failed after %d attempts run_id=%s step=%d: %s",
                        _MAX_STREAM_RETRIES, run_id, step, exc,
                    )
                    raise
                delay = _RETRY_BACKOFF_S[attempt - 1]
                log.warning(
                    "stream dropped (attempt %d/%d) run_id=%s step=%d: %s — retrying in %.0fs",
                    attempt, _MAX_STREAM_RETRIES, run_id, step, exc, delay,
                )
                await asyncio.sleep(delay)

        assert final_message is not None

        usage = final_message.usage
        cache_read: int = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create: int = getattr(usage, "cache_creation_input_tokens", 0) or 0
        total_input_tokens = usage.input_tokens + cache_read + cache_create
        context_tokens = total_input_tokens + usage.output_tokens
        context_pct = context_tokens / self._context_window

        stats = UsageStats(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_create,
            context_pct=context_pct,
            total_input_tokens=total_input_tokens,
            context_tokens=context_tokens,
            context_window=self._context_window,
            context_window_estimated=self._context_window_estimated,
        )
        await bus.publish(LlmUsageEvent(run_id=run_id, ts=_now(), **asdict(stats)))

        tool_calls: list[ToolCallBlock] = []
        thinking_blocks: list[dict[str, object]] = []
        content_blocks: list[dict[str, object]] = []
        for block in final_message.content:
            serialized = block_dict(block)
            content_blocks.append(serialized)
            if block.type == "tool_use":
                tool_calls.append(
                    ToolCallBlock(id=block.id, name=block.name, input=dict(block.input))
                )
            elif block.type in {"thinking", "redacted_thinking"}:
                # thinking blocks must be passed back verbatim in subsequent requests
                thinking_blocks.append(serialized)
                await bus.publish(
                    LlmThinkingEvent(run_id=run_id, block=serialized, ts=_now())
                )

        return LlmResponse(
            stop_reason=final_message.stop_reason or "end_turn",
            tool_calls=tool_calls,
            text="".join(text_parts),
            thinking_blocks=thinking_blocks,
            content_blocks=content_blocks or None,
            usage=stats,
        )
