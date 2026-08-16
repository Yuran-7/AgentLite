from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx
import openai

from agent_lite.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

_DEFAULT_CONTEXT_WINDOW = 200_000
_MAX_STREAM_RETRIES = 3
_RETRY_BACKOFF_S = (1.0, 2.0, 4.0)

_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Use the available tools to complete the user's goal. "
    "When the goal is fully achieved, respond with a final answer and do not call any more tools."
)

log = logging.getLogger(__name__)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 将内部统一工具 schema 转换为 OpenAI Chat Completions 的 function tool 格式
def _convert_tools(tool_schemas: list[dict[str, object]]) -> list[dict[str, object]]:
    tools: list[dict[str, object]] = []
    for schema in tool_schemas:
        function: dict[str, object] = {
            "name": schema["name"],
            "description": schema.get("description", ""),
            "parameters": schema.get("input_schema", {"type": "object"}),
        }
        tools.append({"type": "function", "function": function})
    return tools


# 将内部 Anthropic 风格历史转换为 OpenAI Chat Completions 消息
def _convert_messages(
    messages: list[dict[str, object]],
    system: str | None,
) -> list[dict[str, object]]:
    converted: list[dict[str, object]] = [
        {"role": "system", "content": system or _SYSTEM_PROMPT},
    ]
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            converted.append({"role": role, "content": str(content)})
            continue

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, object]] = []
            for raw_block in content:
                if not isinstance(raw_block, dict):
                    continue
                block: dict[str, object] = raw_block
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(str(block.get("text", "")))
                elif block_type == "tool_use":
                    tool_calls.append(
                        {
                            "id": str(block.get("id", "")),
                            "type": "function",
                            "function": {
                                "name": str(block.get("name", "")),
                                "arguments": json.dumps(
                                    block.get("input", {}), ensure_ascii=False
                                ),
                            },
                        }
                    )
            assistant: dict[str, object] = {
                "role": "assistant",
                "content": "".join(text_parts) or None,
            }
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            converted.append(assistant)
            continue

        text_parts = []
        for raw_block in content:
            if not isinstance(raw_block, dict):
                continue
            block = raw_block
            if block.get("type") == "tool_result":
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id", "")),
                        "content": str(block.get("content", "")),
                    }
                )
            elif block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
        if text_parts:
            converted.append({"role": role, "content": "".join(text_parts)})
    return converted


# 将流式累积的 function arguments 解析为内部工具输入字典
def _parse_tool_input(arguments: str) -> dict[str, object]:
    try:
        value = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {"_raw_arguments": arguments}
    if isinstance(value, dict):
        return value
    return {"value": value}


class OpenAICompatibleProvider:
    # 初始化 OpenAI-compatible 客户端；client 可在测试时注入
    def __init__(
        self,
        model: str,
        client: Any = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if client is None:
            resolved_api_key = api_key or os.environ.get("OPENAI_API_KEY")
            if not resolved_api_key:
                raise SystemExit("OPENAI_API_KEY not set")
            if base_url:
                self._client: Any = openai.AsyncOpenAI(
                    api_key=resolved_api_key,
                    base_url=base_url,
                    max_retries=0,
                )
            else:
                self._client = openai.AsyncOpenAI(
                    api_key=resolved_api_key,
                    max_retries=0,
                )
        else:
            self._client = client
        self._model = model

    # 流式调用 OpenAI-compatible Chat Completions 并转换为内部统一响应
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

        kwargs: dict[str, object] = {
            "model": self._model,
            "messages": _convert_messages(messages, system),
            "max_tokens": 8192,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        tools = _convert_tools(tool_schemas)
        if tools:
            kwargs["tools"] = tools

        text_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        usage_obj: Any = None

        for attempt in range(1, _MAX_STREAM_RETRIES + 1):
            text_parts = []
            tool_parts = {}
            finish_reason = None
            usage_obj = None
            try:
                stream = await self._client.chat.completions.create(**kwargs)
                async for chunk in stream:
                    if getattr(chunk, "usage", None) is not None:
                        usage_obj = chunk.usage
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    choice = choices[0]
                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    text = getattr(delta, "content", None)
                    if text:
                        if attempt == 1:
                            await bus.publish(
                                LlmTokenEvent(run_id=run_id, token=text, ts=_now())
                            )
                        text_parts.append(text)
                    for tool_delta in getattr(delta, "tool_calls", None) or []:
                        index = int(getattr(tool_delta, "index", 0) or 0)
                        part = tool_parts.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        if getattr(tool_delta, "id", None):
                            part["id"] += tool_delta.id
                        function = getattr(tool_delta, "function", None)
                        if function is not None:
                            if getattr(function, "name", None):
                                part["name"] += function.name
                            if getattr(function, "arguments", None):
                                part["arguments"] += function.arguments
                break
            except (
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ConnectError,
                openai.APIConnectionError,
            ) as exc:
                if attempt == _MAX_STREAM_RETRIES:
                    log.error(
                        "stream failed after %d attempts run_id=%s step=%d: %s",
                        _MAX_STREAM_RETRIES,
                        run_id,
                        step,
                        exc,
                    )
                    raise
                delay = _RETRY_BACKOFF_S[attempt - 1]
                log.warning(
                    "stream dropped (attempt %d/%d) run_id=%s step=%d: %s"
                    " — retrying in %.0fs",
                    attempt,
                    _MAX_STREAM_RETRIES,
                    run_id,
                    step,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        tool_calls = [
            ToolCallBlock(
                id=part["id"] or f"call_{index}",
                name=part["name"],
                input=_parse_tool_input(part["arguments"]),
            )
            for index, part in sorted(tool_parts.items())
        ]

        usage: UsageStats | None = None
        if usage_obj is not None:
            input_tokens = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage_obj, "completion_tokens", 0) or 0)
            prompt_details = getattr(usage_obj, "prompt_tokens_details", None)
            cache_read = int(getattr(prompt_details, "cached_tokens", 0) or 0)
            usage = UsageStats(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read,
                context_pct=input_tokens / _DEFAULT_CONTEXT_WINDOW,
            )
            await bus.publish(
                LlmUsageEvent(
                    run_id=run_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_input_tokens=cache_read,
                    cache_creation_input_tokens=0,
                    context_pct=usage.context_pct,
                    ts=_now(),
                )
            )

        stop_reason = "tool_use" if tool_calls else "end_turn"
        if finish_reason == "length":
            stop_reason = "max_tokens"

        return LlmResponse(
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            text="".join(text_parts),
            usage=usage,
        )
