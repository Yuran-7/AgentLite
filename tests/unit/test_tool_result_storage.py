from __future__ import annotations

import copy
from pathlib import Path

import pytest

from agent_lite.core.bus.events import ToolCallFinishedEvent
from agent_lite.core.context import ExecutionContext
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.types import LlmResponse, ToolCallBlock
from agent_lite.core.loop import AgentLoop
from agent_lite.core.session.ids import new_session_id
from agent_lite.core.session.store import SessionStore
from agent_lite.core.tools.base import BaseTool, ToolResult
from agent_lite.core.tools.builtin.read_file import ReadFileTool
from agent_lite.core.tools.invocation import invoke_tool
from agent_lite.core.tools.registry import ToolRegistry
from agent_lite.core.tools.result_storage import ToolResultStore


# 功能：验证大正文独立保存、Unicode 预览含头尾且文件名不能逃出结果目录
# 设计：混合中英文与路径穿越调用 ID，检查正文无损、预览有界和稳定重放
def test_large_result_is_saved_and_preview_is_stable(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path, limit_chars=100, keep_chars=200, token_limit=300)
    text = "HEAD\n" + "中间数据\n" * 1000 + "TAIL\n"
    result = store.prepare(text, "run", "../../escape")
    assert result.truncated
    assert result.output_path is not None
    path = Path(result.output_path)
    assert path.parent == tmp_path / "tool-results"
    assert path.read_bytes() == text.encode("utf-8")
    assert "HEAD" in result.content and "TAIL" in result.content
    assert "Full tool output saved to:" in result.content
    assert "start_line/end_line" in result.content
    assert len(result.content.encode("utf-8")) <= 1200
    assert "\ufffd" not in result.content
    assert store.prepare(text, "run", "../../escape") == result
    assert len(list(path.parent.iterdir())) == 1


# 功能：验证落盘失败仍只发送有限预览，且不假称全文已保存
# 设计：把结果目录位置占用成普通文件，真实触发文件系统失败
def test_storage_failure_does_not_send_full_output(tmp_path: Path) -> None:
    (tmp_path / "tool-results").write_text("blocked", encoding="utf-8")
    result = ToolResultStore(tmp_path, token_limit=200).prepare("x" * 100_000, "r", "t")
    assert result.output_path is None
    assert "could not be saved" in result.content
    assert len(result.content.encode("utf-8")) <= 800


# 功能：验证旧会话迁移后正文仍可找回，新的模型预览跨回放完全一致
# 设计：构造配对工具历史，模拟旧正文、保存新预览和重启存储三个阶段
def test_replay_preserves_model_content(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    sid = new_session_id()
    store.append_message(sid, "assistant", [
        {"type": "tool_use", "id": "call", "name": "read_file", "input": {}},
    ])
    store.append_message(sid, "user", [
        {"type": "tool_result", "tool_use_id": "call", "content": "old" * 10_000},
    ])
    first = store.read_messages(sid)
    assert first == SessionStore(tmp_path).read_messages(sid)
    preview = first[-1]["content"][0]["content"]
    assert "Full tool output saved to:" in preview
    assert len(list(store.session_dir(sid).glob("tool-results/*.txt"))) == 1
    sid2 = new_session_id()
    store.append_messages(sid2, first, "run")
    store.append_message(sid2, "user", "hello")
    assert store.read_messages(sid2)[:-1] == first


class LargeTool(BaseTool):
    name = "large"
    description = "Large test output"
    input_schema = {"type": "object", "properties": {}}

    # 返回带有明确首尾标记的大正文，用于实际 loop 和事件验证
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content="head\n" + "x" * 20_000 + "\ntail")


class RecordingProvider:
    # 保存每次请求的独立副本，避免后续上下文追加影响断言
    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []

    # 首次并发请求四个工具，第二次返回终态，以覆盖当前轮的真正模型输入
    async def chat(self, messages, tool_schemas, bus, run_id, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        if len(self.requests) == 1:
            return LlmResponse(stop_reason="tool_use", tool_calls=[
                ToolCallBlock(id=f"call-{i}", name="large", input={}) for i in range(4)
            ])
        return LlmResponse(stop_reason="end_turn", text="done")


# 功能：验证当前轮先限量再发模型，批量结果有界且事件不重复记录正文
# 设计：跑真实 AgentLoop、EventBus 与四个大工具，核对下一次请求、落盘和跨轮回放
async def test_loop_limits_current_batch_and_persists_same_preview(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    sid = new_session_id()
    result_store = ToolResultStore(
        store.session_dir(sid), token_limit=1000, batch_token_limit=1500,
    )
    provider = RecordingProvider()
    registry = ToolRegistry()
    registry.register(LargeTool())
    events = []
    bus = EventBus()

    # 捕获事件副本以检查持久化层将收到的输出和正文路径
    async def collect(event) -> None:
        events.append(event)

    bus.subscribe(collect)
    context = ExecutionContext(run_id="run", goal="test", max_steps=3)
    await AgentLoop(provider, registry, bus, result_store=result_store).run(context)
    results = provider.requests[1][-1]["content"]
    assert len(results) == 4
    assert sum(len(b["content"].encode("utf-8")) for b in results) <= 1500 * 4
    finished = [e for e in events if isinstance(e, ToolCallFinishedEvent)]
    assert len(finished) == 4
    for event, block in zip(finished, results, strict=True):
        assert event.output == block["content"]
        assert event.truncated
        assert Path(event.output_path).read_text(encoding="utf-8").endswith("tail")
    store.append_messages(sid, context.messages, "run")
    store.append_message(sid, "user", "hello")
    assert store.read_messages(sid)[:-1] == context.messages


# 功能：验证结果文件按行读取和字面搜索不会重新返回全文
# 设计：读取正文中间范围并对 JSONL 做带上限的搜索，检查行号与继续读取提示
async def test_read_result_ranges_and_search(tmp_path: Path) -> None:
    path = tmp_path / "result.txt"
    path.write_text("first\nsecond\nthird\nfourth\n", encoding="utf-8")
    tool = ReadFileTool(tmp_path)
    result = await tool.invoke({"path": "result.txt", "start_line": 2, "end_line": 3})
    assert result.content == "2: second\n3: third\n"
    path.write_text('n\n{"type":"usage"}\n{"type":"usage"}\n', encoding="utf-8")
    result = await tool.invoke({"path": "result.txt", "search": '"usage"', "max_matches": 1})
    assert '2: {"type":"usage"}' in result.content
    assert "start_line=3" in result.content
    with pytest.raises(ValueError, match="end_line"):
        await tool.invoke({"path": "result.txt", "start_line": 3, "end_line": 2})


# 功能：验证巨大工具错误在事件与模型结果中都限量，正文落盘且错误类型不丢失
# 设计：绕过重试的 schema_error 返回大文本，覆盖成功路径之外的审计和恢复
async def test_large_error_is_saved_without_retrying_tool(tmp_path: Path) -> None:
    class ErrorTool(LargeTool):
        # 返回无需重试的错误正文，避免测试等待退避
        async def invoke(self, params: dict[str, object]) -> ToolResult:
            return ToolResult("error" * 20_000, is_error=True, error_type="schema_error")

    registry = ToolRegistry()
    registry.register(ErrorTool())
    events = []
    bus = EventBus()

    # 捕获错误事件，确认日志没有重复存储错误全文
    async def collect(event) -> None:
        events.append(event)

    bus.subscribe(collect)
    result = await invoke_tool(
        registry, ToolCallBlock(id="error", name="large", input={}), bus, "run",
        result_store=ToolResultStore(tmp_path),
    )
    assert result.is_error and result.error_type == "schema_error"
    error = next(e for e in events if e.type == "tool.call_failed")
    assert error.error_message == result.content
    assert error.truncated
    assert Path(error.output_path).read_text(encoding="utf-8") == "error" * 20_000
