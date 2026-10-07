from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.provider import AnthropicProvider
from agent_lite.core.llm.types import LlmResponse

# --- helpers -----------------------------------------------------------------


def _make_usage(
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_create: int = 0,
) -> MagicMock:
    u = MagicMock()
    u.input_tokens = input_tokens
    u.output_tokens = output_tokens
    u.cache_read_input_tokens = cache_read
    u.cache_creation_input_tokens = cache_create
    return u


def _make_final(
    stop_reason: str = "end_turn",
    content: list[MagicMock] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
) -> MagicMock:
    msg = MagicMock()
    msg.stop_reason = stop_reason
    msg.content = content or []
    msg.usage = _make_usage(input_tokens, output_tokens, cache_read)
    return msg


class FakeStream:
    """Minimal async context manager that fakes the anthropic streaming interface."""

    def __init__(self, texts: list[str], final: MagicMock) -> None:
        self._texts = texts
        self._final = final

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    # 模拟 SDK 原始事件迭代器，而非只输出文本的 text_stream
    def __aiter__(self):
        # 逐个生成原始文本增量事件
        async def events():
            for text in self._texts:
                yield SimpleNamespace(type="content_block_delta",
                                      delta=SimpleNamespace(type="text_delta", text=text))
        return events()

    @property
    def text_stream(self):  # type: ignore[return]
        async def _gen():
            for t in self._texts:
                yield t

        return _gen()

    async def get_final_message(self) -> MagicMock:
        return self._final


def _make_provider(
    texts: list[str] | None = None,
    stop_reason: str = "end_turn",
    content: list[MagicMock] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
) -> tuple[AnthropicProvider, MagicMock]:
    final = _make_final(stop_reason, content, input_tokens, output_tokens, cache_read)
    client = MagicMock()
    client.messages.stream.return_value = FakeStream(texts or [], final)
    return AnthropicProvider(model="test-model", client=client), client


async def _chat(
    provider: AnthropicProvider,
    messages: list[dict[str, object]] | None = None,
    tool_schemas: list[dict[str, object]] | None = None,
) -> tuple[LlmResponse, list[BaseModel]]:
    collected: list[BaseModel] = []
    bus = EventBus()

    async def _collect(e: BaseModel) -> None:
        collected.append(e)

    bus.subscribe(_collect)
    result = await provider.chat(
        messages=messages or [],
        tool_schemas=tool_schemas or [],
        bus=bus,
        run_id="r1",
    )
    return result, collected


# --- tests -------------------------------------------------------------------


# 功能：验证缓存读写与输出都占上下文，原始费用字段保持独立
# 设计：用缓存占多数的响应和显式窗口，验证事件与返回值使用同一口径
async def test_context_includes_cached_input_and_output() -> None:
    final = _make_final(input_tokens=10_000, output_tokens=422, cache_read=160_000)
    final.usage.cache_creation_input_tokens = 5_000
    client = MagicMock()
    client.messages.stream.return_value = FakeStream([], final)
    provider = AnthropicProvider("test-model", client=client, context_window=200_000)
    result, events = await _chat(provider)
    assert result.usage is not None
    assert result.usage.input_tokens == 10_000
    assert result.usage.total_input_tokens == 175_000
    assert result.usage.context_tokens == 175_422
    assert result.usage.context_pct == pytest.approx(175_422 / 200_000)
    event = next(e for e in events if e.type == "llm.usage")
    assert event.total_input_tokens == result.usage.total_input_tokens
    assert event.context_pct == result.usage.context_pct
    assert event.context_window == 200_000
    assert event.context_window_estimated is False


# 功能：验证未配置窗口时明确标记回退值为估算
# 设计：使用未知模型避免把兼容端点名称误认为可靠的模型容量
async def test_context_window_fallback_is_estimated() -> None:
    provider, _ = _make_provider()
    result, _ = await _chat(provider)
    assert result.usage is not None
    assert result.usage.context_window == 200_000
    assert result.usage.context_window_estimated is True


# 功能：验证每次 chat 调用都发布携带模型名和路由策略的 llm.model_selected 事件
# 设计：检查事件数量为 1 且字段精确匹配，因为 S6 路由报告依赖此事件统计模型使用分布
async def test_model_selected_event_published() -> None:
    provider, _ = _make_provider()
    _, events = await _chat(provider)
    sel = [e for e in events if e.type == "llm.model_selected"]  # type: ignore[attr-defined]
    assert len(sel) == 1
    assert sel[0].model == "test-model"  # type: ignore[attr-defined]
    assert sel[0].strategy == "static"  # type: ignore[attr-defined]
    assert sel[0].run_id == "r1"  # type: ignore[attr-defined]


# 功能：验证流式响应的每个 token 触发独立的 llm.token 事件，内容与顺序均正确
# 设计：使用 FakeStream 控制精确的 token 序列，断言数量和各 token 的值，排除批量合并或跳过情况
async def test_token_events_published_per_chunk() -> None:
    provider, _ = _make_provider(texts=["Hello", " world"])
    _, events = await _chat(provider)
    tokens = [e for e in events if e.type == "llm.token"]  # type: ignore[attr-defined]
    assert len(tokens) == 2
    assert tokens[0].token == "Hello"  # type: ignore[attr-defined]
    assert tokens[1].token == " world"  # type: ignore[attr-defined]


# 功能：验证 llm.usage 事件中的 token 统计字段（input、output、cache_read）正确
# 设计：向 FakeStream 注入固定 usage 值，断言三个字段精确匹配，因为这些字段是 S6 成本计算的数据源
async def test_usage_event_published_after_stream() -> None:
    provider, _ = _make_provider(input_tokens=200, output_tokens=75, cache_read=150)
    _, events = await _chat(provider)
    usage_events = [e for e in events if e.type == "llm.usage"]  # type: ignore[attr-defined]
    assert len(usage_events) == 1
    ue = usage_events[0]
    assert ue.input_tokens == 200  # type: ignore[attr-defined]
    assert ue.output_tokens == 75  # type: ignore[attr-defined]
    assert ue.cache_read_input_tokens == 150  # type: ignore[attr-defined]


# 功能：验证事件发布顺序为 model_selected → token（×N） → usage
# 设计：检查类型列表的首尾元素，聚焦 provider 的时序契约，而非中间 token 事件的顺序（那由流本身决定）
async def test_event_order_model_selected_first_usage_last() -> None:
    provider, _ = _make_provider(texts=["hi"])
    _, events = await _chat(provider)
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert types[0] == "llm.model_selected"
    assert "llm.token" in types
    assert types[-1] == "llm.usage"


# 功能：验证 stop_reason=tool_use 时 final_message content block 被正确解析为 ToolCallBlock
# 设计：注入带 tool_use block 的 final_message，逐字段检查 ToolCallBlock 的 id/name/input，确认解析路径完整
async def test_tool_use_parsed_from_final_message() -> None:
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = "toolu_01"
    tool_block.name = "read_file"
    tool_block.input = {"path": "README.md"}
    provider, _ = _make_provider(stop_reason="tool_use", content=[tool_block])
    result, _ = await _chat(provider)
    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "toolu_01"
    assert tc.name == "read_file"
    assert tc.input == {"path": "README.md"}


# 功能：验证 stop_reason=end_turn 时不产生任何工具调用
# 设计：与 test_tool_use_parsed 互补，确认 stop_reason 决策树的另一侧，防止 end_turn 响应被误解析为 tool_use
async def test_end_turn_produces_no_tool_calls() -> None:
    provider, _ = _make_provider(stop_reason="end_turn")
    result, _ = await _chat(provider)
    assert result.stop_reason == "end_turn"
    assert result.tool_calls == []


# 功能：验证多个流式 token 被正确拼接为 LlmResponse.text 字段
# 设计：三段 token 拼接，检查 result.text 完整字符串，因为 StdoutPrinter 消费 text 字段展示最终输出
async def test_text_accumulated_from_tokens() -> None:
    provider, _ = _make_provider(texts=["foo", "bar", "baz"])
    result, _ = await _chat(provider)
    assert result.text == "foobarbaz"


# 功能：验证缺少 ANTHROPIC_API_KEY 时 provider 初始化立即 SystemExit 而非等到调用时才报错
# 设计：用 monkeypatch 清除环境变量后实例化，确认 fail-fast 行为，防止"幽灵 run"（有 started 但无 finished 事件）
async def test_missing_api_key_raises_system_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        AnthropicProvider(model="any")


# 功能：验证空流式响应不发布任何 llm.token 事件且 result.text 为空字符串
# 设计：texts=[] 覆盖零 token 边界条件，确认 text="" 而非 None，避免调用方对空内容做额外 None 判断
async def test_no_tokens_when_response_is_empty() -> None:
    provider, _ = _make_provider(texts=[])
    result, events = await _chat(provider)
    tokens = [e for e in events if e.type == "llm.token"]  # type: ignore[attr-defined]
    assert tokens == []
    assert result.text == ""


# 功能：完整保留 Claude 空推理签名、遮蔽密文与原始块顺序。
# 设计：真实 SDK 内容块与混合工具响应验证加密字段不会丢失。
async def test_encrypted_thinking_and_redacted_blocks_preserved():
    from anthropic.types import RedactedThinkingBlock, TextBlock, ThinkingBlock, ToolUseBlock


    content = [ThinkingBlock(type="thinking", thinking="", signature="signed"),
               TextBlock(type="text", text="checking"),
               RedactedThinkingBlock(type="redacted_thinking", data="encrypted"),
               ToolUseBlock(type="tool_use", id="call", name="read", input={})]
    provider, _ = _make_provider(texts=["checking"], content=content)
    result, events = await _chat(provider)
    expected = [b.model_dump(mode="json", exclude_none=True) for b in content]
    assert result.content_blocks == expected
    assert result.thinking_blocks == [expected[0], expected[2]]
    assert [e.block for e in events if e.type == "llm.thinking"] == result.thinking_blocks


# 功能：Claude 推理文本以独立事件流输出，不污染回答正文。
# 设计：模拟原始推理和文本增量事件，验证两条流分别收集。
async def test_anthropic_thinking_delta():
    class ThinkingStream(FakeStream):
        # 生成交错的推理与回答事件
        def __aiter__(self):
            # 模拟 SDK 的两类内容块增量
            async def events():
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(
                    type="thinking_delta", thinking="analysis"))
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(
                    type="text_delta", text="answer"))
            return events()
    client = MagicMock()
    client.messages.stream.return_value = ThinkingStream([], _make_final())
    result, events = await _chat(AnthropicProvider("test", client=client))
    assert result.text == "answer"
    assert [e.token for e in events if e.type == "llm.thinking"] == ["analysis"]


# 功能：Claude 推理阶段断流后发布重置事件，成功尝试的签名独立保存。
# 设计：两次尝试都没有正文，用真实推理块覆盖旧 text_stream 重试路径的盲区。
async def test_anthropic_thinking_retry(monkeypatch):
    import httpx
    from anthropic.types import ThinkingBlock

    monkeypatch.setattr("agent_lite.core.llm.provider._RETRY_BACKOFF_S", (0, 0, 0))

    class RetryStream(FakeStream):
        # 按尝试生成推理增量，第一轮以网络错误结束
        def __aiter__(self):
            # 使用签名区分失败和成功尝试
            async def events():
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(
                    type="thinking_delta", thinking=self._texts[0]))
                if self._texts[0] == "old":
                    raise httpx.ReadError("disconnected")
            return events()

    final = _make_final(content=[ThinkingBlock(
        type="thinking", thinking="new", signature="new-signature")])
    client = MagicMock()
    client.messages.stream.side_effect = [RetryStream(["old"], final), RetryStream(["new"], final)]
    result, events = await _chat(AnthropicProvider("test", client=client))
    assert result.thinking_blocks[0]["signature"] == "new-signature"
    assert [(e.token, e.reset) for e in events if e.type == "llm.thinking" and e.block is None] == [
        ("old", False), ("", True), ("new", False)]
