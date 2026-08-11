from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.openai_provider import OpenAICompatibleProvider
from kama_claude.core.llm.types import LlmResponse


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
    assert [event.type for event in events] == [  # type: ignore[attr-defined]
        "llm.model_selected",
        "llm.token",
        "llm.token",
        "llm.usage",
    ]


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
