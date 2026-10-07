from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, cast

import httpx
import openai

from agent_lite.core.bus.events import (
    LlmModelSelectedEvent,
    LlmThinkingEvent,
    LlmTokenEvent,
    LlmUsageEvent,
)
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.openai_provider import (
    _MAX_STREAM_RETRIES,
    _RETRY_BACKOFF_S,
    _SYSTEM_PROMPT,
    OpenAICompatibleProvider,
    _convert_messages,
    _convert_tools,
    _now,
    _parse_tool_input,
    log,
)
from agent_lite.core.llm.thinking import block_dict
from agent_lite.core.llm.types import LlmResponse, ToolCallBlock, UsageStats


# 转换历史消息并按轮次保留 Responses 原生推理项
def _convert_input(messages: list[dict[str, object]]) -> list[dict[str, Any]]:
    """Convert expanded history to Responses items, retaining function call IDs."""
    items: list[dict[str, Any]] = []
    for original in messages:
        original_content = original.get("content", [])
        if original.get("role") == "assistant" and isinstance(original_content, list):
            for block in original_content:
                if isinstance(block, dict) and block.get("type") == "responses_reasoning":
                    items.append(cast(dict[str, Any], block["item"]))
        items.extend(_convert_input_message(original))
    return items


# 转换单条消息的文本和工具调用，推理项由外层按轮次插入
def _convert_input_message(original: dict[str, object]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in _convert_messages([original], None)[1:]:
        role = message["role"]
        if role == "tool":
            items.append({"type": "function_call_output",
                          "call_id": message["tool_call_id"], "output": message["content"]})
            continue
        content = message.get("content")
        if content:
            if isinstance(content, list):
                parts = []
                for part in content:
                    if part["type"] == "image_url":
                        parts.append({"type": "input_image",
                                      "image_url": part["image_url"]["url"]})
                    else:
                        parts.append({"type": "input_text", "text": part["text"]})
                content = parts
            items.append({"role": role, "content": content})
        for call in cast(list[dict[str, Any]], message.get("tool_calls", [])):
            items.append({"type": "function_call", "call_id": call["id"],
                          "name": call["function"]["name"],
                          "arguments": call["function"]["arguments"]})
    return items


class OpenAIResponsesProvider(OpenAICompatibleProvider):
    """Responses transport using local conversation history rather than server state."""

    async def chat(
        self, messages: list[dict[str, object]], tool_schemas: list[dict[str, object]],
        bus: EventBus, run_id: str, *, step: int = 0, system: str | None = None,
    ) -> LlmResponse:
        await bus.publish(
            LlmModelSelectedEvent(run_id=run_id, model=self._model, strategy="static", ts=_now())
        )
        kwargs: dict[str, Any] = {
            "model": self._model, "input": _convert_input(messages),
            "instructions": system or _SYSTEM_PROMPT, "stream": True, "store": False,
            "max_output_tokens": 8192,
            "include": ["reasoning.encrypted_content"],
        }
        tools = _convert_tools(tool_schemas)
        if self._reasoning_effort:
            kwargs["reasoning"] = {"effort": self._reasoning_effort}
        if tools:
            # Responses uses flat function definitions; arbitrary local schemas are non-strict.
            kwargs["tools"] = [
                {"type": "function", **cast(dict[str, Any], tool["function"]), "strict": False}
                for tool in tools
            ]

        published_text = False
        published_thinking = False
        for attempt in range(1, _MAX_STREAM_RETRIES + 1):
            text_parts: list[str] = []
            calls: dict[int, dict[str, str]] = {}
            response: Any = None
            stream: Any = None
            reasoning_items: dict[int, dict[str, Any]] = {}
            if published_thinking:
                await bus.publish(LlmThinkingEvent(run_id=run_id, reset=True, ts=_now()))
                published_thinking = False
            if published_text:
                await bus.publish(LlmTokenEvent(run_id=run_id, token="", reset=True, ts=_now()))
                published_text = False
            try:
                stream = await self._client.responses.create(**kwargs)
                async for event in stream:
                    kind = event.type
                    if kind == "response.output_text.delta":
                        text_parts.append(event.delta)
                        await bus.publish(
                            LlmTokenEvent(run_id=run_id, token=event.delta, ts=_now())
                        )
                        published_text = True
                    elif kind in {
                        "response.reasoning_summary_text.delta", "response.reasoning_text.delta",
                    }:
                        await bus.publish(
                            LlmThinkingEvent(run_id=run_id, token=event.delta, ts=_now())
                        )
                        published_thinking = True
                    elif kind in {"response.output_item.added", "response.output_item.done"}:
                        item = event.item
                        if item.type == "reasoning":
                            reasoning_items[event.output_index] = block_dict(item)
                        if item.type == "function_call":
                            part = calls.setdefault(event.output_index, {})
                            part.update(id=item.call_id, name=item.name)
                            if kind.endswith("done") or getattr(item, "arguments", ""):
                                part["arguments"] = item.arguments
                    elif kind == "response.function_call_arguments.delta":
                        part = calls.setdefault(event.output_index, {})
                        part["arguments"] = part.get("arguments", "") + event.delta
                    elif kind == "response.function_call_arguments.done":
                        calls.setdefault(event.output_index, {})["arguments"] = event.arguments
                    elif kind in {"response.completed", "response.incomplete"}:
                        response = event.response
                    elif kind in {"response.failed", "error"}:
                        raise RuntimeError("Responses API returned a failed response")
                if response is None:
                    raise httpx.ReadError("Responses stream ended without a terminal event")
                if response.status not in {"completed", "incomplete"}:
                    raise RuntimeError("Responses API returned an unsuccessful status")
                # Terminal output is authoritative, including calls without delta events.
                for index, item in enumerate(response.output):
                    if item.type == "reasoning":
                        reasoning_items[index] = block_dict(item)
                    if item.type == "function_call":
                        calls[index] = {"id": item.call_id, "name": item.name,
                                        "arguments": item.arguments}
                break
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError,
                    openai.APIConnectionError):
                if attempt == _MAX_STREAM_RETRIES:
                    raise
                log.warning("Responses stream dropped run_id=%s step=%d attempt=%d",
                            run_id, step, attempt)
                await asyncio.sleep(_RETRY_BACKOFF_S[attempt - 1])
            finally:
                if stream is not None:
                    await stream.close()

        tool_calls = [ToolCallBlock(id=part["id"], name=part["name"],
                                   input=_parse_tool_input(part.get("arguments", "")))
                      for _, part in sorted(calls.items())]
        usage = None
        if response.usage is not None:
            raw = response.usage
            input_tokens, output_tokens = raw.input_tokens, raw.output_tokens
            usage = UsageStats(
                input_tokens=input_tokens, output_tokens=output_tokens,
                cache_read_input_tokens=getattr(raw.input_tokens_details, "cached_tokens", 0),
                total_input_tokens=input_tokens, context_tokens=input_tokens + output_tokens,
                context_pct=(input_tokens + output_tokens) / self._context_window,
                context_window=self._context_window,
                context_window_estimated=self._context_window_estimated,
                reasoning_output_tokens=getattr(
                    getattr(raw, "output_tokens_details", None), "reasoning_tokens", None,
                ),
            )
            await bus.publish(LlmUsageEvent(run_id=run_id, ts=_now(), **asdict(usage)))
        stop_reason = "tool_use" if tool_calls else "end_turn"
        if response.status == "incomplete":
            reason = getattr(response.incomplete_details, "reason", None)
            if reason != "max_output_tokens":
                raise RuntimeError(f"Responses API returned an incomplete response: {reason}")
            stop_reason = "max_tokens"
        thinking_blocks: list[dict[str, object]] = [
            {"type": "responses_reasoning", "item": item}
            for _, item in sorted(reasoning_items.items())
        ]
        for block in thinking_blocks:
            await bus.publish(LlmThinkingEvent(run_id=run_id, block=block, ts=_now()))
        return LlmResponse(stop_reason=stop_reason, tool_calls=tool_calls,
                           text="".join(text_parts), usage=usage,
                           thinking_blocks=thinking_blocks)
