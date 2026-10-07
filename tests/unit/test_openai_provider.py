from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import BaseModel

from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.openai_provider import OpenAICompatibleProvider
from agent_lite.core.llm.types import LlmResponse


class FakeOpenAIStream:
    # 保存待返回的 OpenAI-compatible 流式 chunk
    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self._chunks = iter(chunks)

    # 返回异步迭代器自身
    def __aiter__(self) -> FakeOpenAIStream:
        return self

    # 按顺序返回下一个流式 chunk
    async def __anext__(self) -> SimpleNamespace:
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeDroppedOpenAIStream(FakeOpenAIStream):
    async def __anext__(self) -> SimpleNamespace:
        try:
            return await super().__anext__()
        except StopAsyncIteration as exc:
            raise httpx.ReadError("stream disconnected") from exc


# 构造只含文本增量的 Chat Completions chunk
def _text_chunk(text: str, finish_reason: str | None = None) -> SimpleNamespace:
    delta = SimpleNamespace(content=text, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


# 构造只含工具调用增量的 Chat Completions chunk
def _tool_chunk(
    *,
    call_id: str | None,
    name: str | None,
    arguments: str | None,
    finish_reason: str | None = None,
) -> SimpleNamespace:
    function = SimpleNamespace(name=name, arguments=arguments)
    tool_call = SimpleNamespace(index=0, id=call_id, function=function)
    delta = SimpleNamespace(content=None, tool_calls=[tool_call])
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


# 构造带 token 用量且无 choices 的流式收尾 chunk
def _usage_chunk(input_tokens: int, output_tokens: int, cached_tokens: int) -> SimpleNamespace:
    details = SimpleNamespace(cached_tokens=cached_tokens)
    usage = SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        prompt_tokens_details=details,
    )
    return SimpleNamespace(choices=[], usage=usage)


# 使用注入的假客户端创建 Provider，避免发起真实网络请求
def _make_provider(chunks: list[SimpleNamespace]) -> tuple[OpenAICompatibleProvider, MagicMock]:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=FakeOpenAIStream(chunks))
    return OpenAICompatibleProvider("deepseek-test", client=client), client


# 调用 Provider 并收集 EventBus 发布的所有事件
async def _chat(
    provider: OpenAICompatibleProvider,
    messages: list[dict[str, object]] | None = None,
    tool_schemas: list[dict[str, object]] | None = None,
) -> tuple[LlmResponse, list[BaseModel]]:
    events: list[BaseModel] = []
    bus = EventBus()

    # 收集 Provider 发布的事件供断言使用
    async def _collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(_collect)
    result = await provider.chat(
        messages=messages or [],
        tool_schemas=tool_schemas or [],
        bus=bus,
        run_id="r-openai",
    )
    return result, events


# 功能：验证 OpenAI-compatible 文本流被拼接并发布 token 与 usage 事件
# 设计：注入两个文本 chunk 和独立 usage 收尾 chunk，覆盖标准流式响应的完整时序
async def test_openai_text_stream_and_usage_events() -> None:
    provider, _ = _make_provider(
        [_text_chunk("Hello"), _text_chunk(" DeepSeek", "stop"), _usage_chunk(120, 8, 20)]
    )

    result, events = await _chat(provider)

    assert result.stop_reason == "end_turn"
    assert result.text == "Hello DeepSeek"
    assert result.usage is not None
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 8
    assert result.usage.cache_read_input_tokens == 20
    assert result.usage.total_input_tokens == 120
    assert result.usage.context_tokens == 128
    assert result.usage.context_pct == pytest.approx(128 / 200_000)
    assert result.usage.context_window_estimated is True
    assert [event.type for event in events] == [  # type: ignore[attr-defined]
        "llm.model_selected",
        "llm.token",
        "llm.token",
        "llm.usage",
    ]


# 功能：验证截图中的输入和缓存不会重复计数，且窗口可按模型配置
# 设计：让缓存接近输入总量，用 128K 显式窗口检查响应和事件占用一致
async def test_custom_window_does_not_double_count_cached_prompt() -> None:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=FakeOpenAIStream([
        _usage_chunk(21_024, 422, 19_968),
    ]))
    provider = OpenAICompatibleProvider("custom", client=client, context_window=128_000)
    result, events = await _chat(provider)
    assert result.usage is not None
    assert result.usage.total_input_tokens == 21_024
    assert result.usage.context_tokens == 21_446
    assert result.usage.context_pct == pytest.approx(21_446 / 128_000)
    event = next(e for e in events if e.type == "llm.usage")
    assert event.context_pct == result.usage.context_pct
    assert event.context_window_estimated is False


@pytest.mark.parametrize("partial", [False, True])
async def test_retry_publishes_complete_response_without_stale_text(
    monkeypatch: pytest.MonkeyPatch, partial: bool
) -> None:
    monkeypatch.setattr(
        "agent_lite.core.llm.openai_provider._RETRY_BACKOFF_S", (0.0, 0.0, 0.0)
    )
    first_chunks = [_text_chunk("stale")] if partial else []
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[
            FakeDroppedOpenAIStream(first_chunks),
            FakeOpenAIStream([_text_chunk("recovered", "stop")]),
        ]
    )
    provider = OpenAICompatibleProvider("test-model", client=client)

    result, events = await _chat(provider)

    assert result.text == "recovered"
    tokens = [event for event in events if event.type == "llm.token"]
    expected = [("stale", False), ("", True), ("recovered", False)] if partial else [
        ("recovered", False)
    ]
    assert [(event.token, event.reset) for event in tokens] == expected
    assert client.chat.completions.create.await_count == 2


# 功能：验证分片返回的 OpenAI function call 被合并为内部 ToolCallBlock
# 设计：将 JSON arguments 拆成两个 chunk，确认 call ID、名称和参数都能无损重组
async def test_openai_streamed_tool_call_is_reassembled() -> None:
    provider, _ = _make_provider(
        [
            _tool_chunk(call_id="call_1", name="read_file", arguments='{"pa'),
            _tool_chunk(
                call_id=None,
                name=None,
                arguments='th":"README.md"}',
                finish_reason="tool_calls",
            ),
        ]
    )

    result, _ = await _chat(provider)

    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].input == {"path": "README.md"}


# 功能：验证内部工具 schema 和 Anthropic 风格历史在请求边界转换为 OpenAI 格式
# 设计：同时传入 assistant tool_use、user tool_result 和工具定义，检查发送给假客户端的最终 kwargs
async def test_openai_request_converts_tools_and_history() -> None:
    provider, client = _make_provider([_text_chunk("done", "stop")])
    messages: list[dict[str, object]] = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {
                    "type": "tool_use",
                    "id": "call_old",
                    "name": "read_file",
                    "input": {"path": "a.txt"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_old",
                    "content": "file body",
                }
            ],
        },
    ]
    schemas = [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]

    await _chat(provider, messages, schemas)

    kwargs = client.chat.completions.create.await_args.kwargs
    assert kwargs["model"] == "deepseek-test"
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tools"][0]["function"]["parameters"] == schemas[0]["input_schema"]
    assert kwargs["messages"][1]["role"] == "assistant"
    assert kwargs["messages"][1]["tool_calls"][0]["id"] == "call_old"
    assert kwargs["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_old",
        "content": "file body",
    }


# 功能：验证 OpenAI-compatible Provider 缺少密钥时立即失败
# 设计：清除标准密钥后直接构造 Provider，确保错误发生在创建 run 之前
def test_openai_missing_api_key_raises_system_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
        OpenAICompatibleProvider("deepseek-test")


# 功能：推理增量写入事件与最终响应，并在下一轮按原字段回传。
# 设计：覆盖三个兼容字段别名，正文与推理同时到达时均须保留。
@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "thinking"])
async def test_reasoning_stream_roundtrip(field):
    first = _text_chunk("answer", "length")
    setattr(first.choices[0].delta, field, "checking")
    provider, client = _make_provider([first, _usage_chunk(10, 8192, 0)])
    result, events = await _chat(provider)
    assert result.text == "answer"
    assert result.stop_reason == "max_tokens"
    assert result.thinking_blocks == [{"type": "reasoning_content", "field": field,
                                       "text": "checking"}]
    thinking = [e for e in events if e.type == "llm.thinking"]
    assert thinking[0].token == "checking"
    assert thinking[1].block == result.thinking_blocks[0]
    assert result.usage.reasoning_output_tokens is None
    client.chat.completions.create.return_value = FakeOpenAIStream([_text_chunk("done")])
    await _chat(provider, [{"role": "assistant", "content": result.thinking_blocks +
                           [{"type": "text", "text": result.text}]}])
    assert client.chat.completions.create.await_args.kwargs["messages"][1][field] == "checking"


# 功能：推理阶段断流重试后只保留成功尝试的内容。
# 设计：第一次只有推理没有正文，确认仍发布推理重置事件。
async def test_reasoning_retry_resets(monkeypatch):
    monkeypatch.setattr("agent_lite.core.llm.openai_provider._RETRY_BACKOFF_S", (0, 0, 0))
    old, new = _text_chunk(""), _text_chunk("done")
    old.choices[0].delta.reasoning_content = "old"
    new.choices[0].delta.reasoning_content = "new"
    provider, client = _make_provider([])
    client.chat.completions.create.side_effect = [FakeDroppedOpenAIStream([old]),
                                                 FakeOpenAIStream([new])]
    result, events = await _chat(provider)
    assert result.thinking_blocks[0]["text"] == "new"
    assert [(e.token, e.reset) for e in events if e.type == "llm.thinking" and e.block is None] == [
        ("old", False), ("", True), ("new", False)]
