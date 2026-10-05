import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest

from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.responses_provider import OpenAIResponsesProvider, _convert_input


class Stream:
    def __init__(self, events, dropped=False):
        self.events = iter(events)
        self.dropped = dropped
        self.close = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.events)
        except StopIteration:
            if self.dropped:
                raise httpx.ReadError("disconnected") from None
            raise StopAsyncIteration from None


def terminal(status="completed", output=None, reason=None):
    return NS(type="response." + status, response=NS(
        status=status, output=output or [], incomplete_details=NS(reason=reason),
        usage=NS(input_tokens=120, output_tokens=8, input_tokens_details=NS(cached_tokens=100)),
    ))


async def run(streams):
    client = MagicMock()
    client.responses.create = AsyncMock(side_effect=streams)
    provider = OpenAIResponsesProvider("test", client=client, context_window=1000)
    bus = EventBus()
    events = []

    async def collect(event):
        events.append(event)

    bus.subscribe(collect)
    result = await provider.chat([], [{"name": "read", "input_schema": {"type": "object"}}],
                                 bus, "run", system="custom instructions")
    return result, events, client


# 功能：验证 Responses 文本、并行工具参数和用量转换。
# 设计：工具分片与最终完整项同时出现，确保不会重复拼接参数或缓存 token。
async def test_stream_tools_usage_and_request():
    call = NS(type="function_call", id="fc_1", call_id="call_1", name="read", arguments="")
    done = NS(type="function_call", id="fc_1", call_id="call_1", name="read",
              arguments='{"path":"a"}')
    stream = Stream([
        NS(type="response.output_text.delta", delta="hello"),
        NS(type="response.output_item.added", output_index=1, item=call),
        NS(type="response.function_call_arguments.delta", output_index=1, delta='{"path":'),
        NS(type="response.function_call_arguments.delta", output_index=1, delta='"a"}'),
        NS(type="response.output_item.done", output_index=1, item=done),
        terminal(output=[NS(type="message"), done]),
    ])
    result, events, client = await run([stream])
    assert result.text == "hello"
    assert result.stop_reason == "tool_use"
    assert [(c.id, c.name, c.input) for c in result.tool_calls] == [("call_1", "read", {"path": "a"})]
    assert result.usage.context_tokens == 128
    assert result.usage.cache_read_input_tokens == 100
    assert result.usage.context_pct == pytest.approx(.128)
    kwargs = client.responses.create.await_args.kwargs
    assert kwargs["instructions"] == "custom instructions"
    assert kwargs["store"] is False
    assert kwargs["tools"][0] == {"type": "function", "name": "read", "description": "",
                                  "parameters": {"type": "object"}, "strict": False}
    assert "messages" not in kwargs and "stream_options" not in kwargs
    assert [e.type for e in events] == ["llm.model_selected", "llm.token", "llm.usage"]
    stream.close.assert_awaited_once()


# 功能：验证工具历史和图片输入符合 Responses item 格式。
# 设计：同一轮包含文字、工具结果和图片，检查 call_id 与图像数据保留。
def test_history_and_images():
    items = _convert_input([
        {"role": "assistant", "content": [{"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "call_old", "name": "read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_old",
            "content": "body"}, {"type": "image", "source": {"type": "base64",
            "media_type": "image/png", "data": "abc"}}]},
    ])
    assert items[0] == {"role": "assistant", "content": "checking"}
    assert items[1] == {"type": "function_call", "call_id": "call_old", "name": "read", "arguments": "{}"}
    assert items[2] == {"type": "function_call_output", "call_id": "call_old", "output": "body"}
    assert items[3]["content"] == [{"type": "input_image", "image_url": "data:image/png;base64,abc"}]


# 功能：断流或缺少终止事件时重试并清理旧文本。
# 设计：第一次输出部分文字后断开，第二次完成，确认界面收到 reset 事件。
@pytest.mark.parametrize("dropped", [True, False])
async def test_retry_and_reset(monkeypatch, dropped):
    monkeypatch.setattr("agent_lite.core.llm.responses_provider._RETRY_BACKOFF_S", (0, 0, 0))
    first = Stream([NS(type="response.output_text.delta", delta="old")], dropped=dropped)
    second = Stream([NS(type="response.output_text.delta", delta="new"), terminal()])
    result, events, client = await run([first, second])
    assert result.text == "new"
    assert [(e.token, e.reset) for e in events if e.type == "llm.token"] == [("old", False), ("", True), ("new", False)]
    assert client.responses.create.await_count == 2
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()


# 功能：区分 token 截断与服务端失败，避免把失败当作正常结束。
# 设计：分别返回 incomplete、failed 和 error 事件，覆盖不同终止原因。
@pytest.mark.parametrize("kind", ["max_output_tokens", "content_filter", "failed", "error"])
async def test_terminal_status(kind):
    event = NS(type="response.failed" if kind == "failed" else "error") if kind in {"failed", "error"} else terminal("incomplete", reason=kind)
    if kind == "max_output_tokens":
        result, _, _ = await run([Stream([event])])
        assert result.stop_reason == "max_tokens"
    else:
        with pytest.raises(RuntimeError):
            await run([Stream([event])])


# 功能：验证真实 OpenAI SDK 的 Responses 路径和 SSE 解码兼容。
# 设计：使用 MockTransport 截获 HTTP，覆盖 SDK 层而不调用收费接口。
async def test_real_sdk_transport():
    requests = []

    def handle(request):
        requests.append(request)
        response = {
            "id": "resp_test", "object": "response", "created_at": 1,
            "model": "test", "status": "completed", "output": [],
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                      "input_tokens_details": {"cached_tokens": 5},
                      "output_tokens_details": {"reasoning_tokens": 0}},
        }
        events = [
            {"type": "response.output_text.delta", "delta": "hello", "item_id": "msg_1",
             "output_index": 0, "content_index": 0, "sequence_number": 1},
            {"type": "response.completed", "response": response, "sequence_number": 2},
        ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        async with openai.AsyncOpenAI(api_key="test", base_url="https://test.invalid/codex/v1",
                                     http_client=http) as client:
            provider = OpenAIResponsesProvider("test", client=client)
            result = await provider.chat([{"role": "user", "content": "hi"}], [], EventBus(), "r")
    assert str(requests[0].url) == "https://test.invalid/codex/v1/responses"
    assert json.loads(requests[0].content)["input"] == [{"role": "user", "content": "hi"}]
    assert result.text == "hello"
    assert result.usage.cache_read_input_tokens == 5
