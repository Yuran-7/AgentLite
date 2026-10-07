from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import openai
from pydantic import TypeAdapter

from agent_lite.core.bus.events import Event
from agent_lite.core.context import ExecutionContext
from agent_lite.core.events.bus import EventBus
from agent_lite.core.events.writer import EventWriter
from agent_lite.core.llm.openai_provider import OpenAICompatibleProvider
from agent_lite.core.llm.types import LlmResponse
from agent_lite.core.loop import AgentLoop
from agent_lite.core.tools.registry import ToolRegistry
from agent_lite.core.trace.provider import TracingProvider
from agent_lite.core.trace.writer import TraceWriter


# 功能：真实 SDK 收到纯推理截断流后，事件与 trace 均保存推理文本及用量。
# 设计：MockTransport 模拟 8192 输出且没有正文，通过磁盘 JSONL 验证整条收集路径。
async def test_sdk_reasoning_only_stream_saved(tmp_path: Path) -> None:
    requests: list[dict] = []

    # 用本地 SSE 替身模拟开放模型的额外 reasoning_content 字段
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        chunks = [
            {"choices": [{"index": 0, "delta": {"reasoning_content": "思考中"},
                          "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
            {"choices": [], "usage": {"prompt_tokens": 57, "completion_tokens": 8192,
                "total_tokens": 8249, "completion_tokens_details": {"reasoning_tokens": 8192}}},
        ]
        for chunk in chunks:
            chunk.update(id="chat_test", object="chat.completion.chunk", created=1, model="test")
        body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    bus = EventBus()
    trace = TraceWriter(tmp_path / "trace.jsonl")
    await trace.start()
    try:
        async with EventWriter(tmp_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                async with openai.AsyncOpenAI(api_key="test", http_client=http) as client:
                    provider = TracingProvider(OpenAICompatibleProvider("test", client=client), trace)
                    result = await provider.chat([], [], bus, "r")
    finally:
        await trace.stop()
    assert result.text == ""
    assert result.stop_reason == "max_tokens"
    assert result.thinking_blocks[0]["text"] == "思考中"
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text(
        encoding="utf-8").splitlines()]
    adapter = TypeAdapter(Event)
    for row in rows:
        adapter.validate_python(row)
    assert [row["token"] for row in rows if row["type"] == "llm.thinking"
            and row["block"] is None] == ["思考中"]
    usage = next(row for row in rows if row["type"] == "llm.usage")
    assert usage["output_tokens"] == usage["reasoning_output_tokens"] == 8192
    traced = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert traced[-1]["data"]["thinking_blocks"] == result.thinking_blocks


# 功能：AgentLoop 将 Claude 原始块顺序保留到可持久化历史。
# 设计：文本夹在两个加密块之间，验证循环不会把推理集中到正文之前。
async def test_loop_preserves_original_thinking_order() -> None:
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "signed"},
        {"type": "text", "text": "checking"},
        {"type": "redacted_thinking", "data": "cipher"},
    ]
    provider = AsyncMock()
    provider.chat.return_value = LlmResponse(
        stop_reason="end_turn", text="checking", content_blocks=blocks,
        thinking_blocks=[blocks[0], blocks[2]],
    )
    context = ExecutionContext(run_id="r", goal="test", max_steps=1)
    await AgentLoop(provider, ToolRegistry(), EventBus()).run(context)
    assert context.persistence_messages(1)[0]["content"] == blocks
