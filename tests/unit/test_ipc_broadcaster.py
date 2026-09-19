from __future__ import annotations

import asyncio
import json
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from agent_lite.core.bus.events import RunStartedEvent, StepStartedEvent
from agent_lite.core.transport.ipc_broadcaster import IpcEventBroadcaster


def _make_writer(*, drain_raises: Exception | None = None) -> asyncio.StreamWriter:
    writer = MagicMock(spec=asyncio.StreamWriter)
    if drain_raises is not None:
        writer.drain = AsyncMock(side_effect=drain_raises)
    else:
        writer.drain = AsyncMock()
    return cast(asyncio.StreamWriter, writer)


def _run_started(run_id: str = "r1", session_id: str | None = None) -> RunStartedEvent:
    return RunStartedEvent(
        run_id=run_id,
        session_id=session_id,
        goal="test",
        ts="2026-01-01T00:00:00Z",
    )


# 功能：验证 subscribe 后 handle 将匹配 topic 的事件写入 JSON-RPC Notification
# 设计：用 MagicMock writer 捕获写入的字节，检查 event.push 的完整 wire 结构
async def test_subscriber_receives_matching_event() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"])

    await broadcaster.handle(_run_started())

    writer.write.assert_called_once()  # type: ignore[attr-defined]
    data = json.loads(writer.write.call_args[0][0].rstrip(b"\n"))  # type: ignore[attr-defined]
    assert data["jsonrpc"] == "2.0"
    assert data["method"] == "event.push"
    assert data["params"]["type"] == "run.started"
    assert "id" not in data


# 功能：验证无订阅时 handle 不向任何 writer 写入数据
# 设计：创建 broadcaster 但不 subscribe，调用 handle 后断言 write 从未被调用，验证空 fan-out 的边界情况
async def test_no_subscription_no_write() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()

    await broadcaster.handle(_run_started())

    writer.write.assert_not_called()  # type: ignore[attr-defined]


# 功能：验证 topic glob "step.*" 匹配 step.started 但不匹配 run.started
# 设计：向同一 broadcaster 发布两种事件，断言 write 只被调用一次，验证 fnmatch 语义的 glob 边界行为
async def test_topic_glob_matches_step_not_run() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["step.*"])

    step_event = StepStartedEvent(run_id="r1", step=1, ts="2026-01-01T00:00:00Z")
    run_event = _run_started()

    await broadcaster.handle(step_event)
    await broadcaster.handle(run_event)

    assert writer.write.call_count == 1  # type: ignore[attr-defined]
    data = json.loads(writer.write.call_args[0][0].rstrip(b"\n"))  # type: ignore[attr-defined]
    assert data["params"]["type"] == "step.started"


# 功能：验证 scope="global" 的订阅能收到任意 run_id 的事件
# 设计：发布两个不同 run_id 的事件，断言两次都写入，确认 global scope 不过滤 run_id 字段
async def test_scope_global_receives_all_run_ids() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"], scope="global")

    await broadcaster.handle(_run_started("r1"))
    await broadcaster.handle(_run_started("r2"))

    assert writer.write.call_count == 2  # type: ignore[attr-defined]


# 功能：验证 scope="run:<id>" 只接收匹配 run_id 的事件，过滤其他 run_id
# 设计：订阅 scope="run:abc"，发布 run_id="abc" 和 run_id="xyz"，断言只写入一次，验证 run-specific scope 的过滤语义
async def test_scope_run_specific_filters_other_run_ids() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"], scope="run:abc")

    await broadcaster.handle(_run_started("abc"))
    await broadcaster.handle(_run_started("xyz"))

    assert writer.write.call_count == 1  # type: ignore[attr-defined]


# 功能：验证不同 session scope 的订阅者只收到各自 session 及其后续 run 事件
# 设计：先用 run.started 建立 run 到 session 的映射，再发布仅含 run_id 的 step 事件验证持续隔离
async def test_scope_session_specific_filters_other_sessions() -> None:
    broadcaster = IpcEventBroadcaster()
    writer_a = _make_writer()
    writer_b = _make_writer()
    broadcaster.subscribe(writer_a, topics=["run.*", "step.*"], scope="session:a")
    broadcaster.subscribe(writer_b, topics=["run.*", "step.*"], scope="session:b")

    await broadcaster.handle(_run_started("run-a", "a"))
    await broadcaster.handle(_run_started("run-b", "b"))
    await broadcaster.handle(
        StepStartedEvent(run_id="run-a", step=1, ts="2026-01-01T00:00:00Z")
    )

    assert writer_a.write.call_count == 2  # type: ignore[attr-defined]
    assert writer_b.write.call_count == 1  # type: ignore[attr-defined]


# 功能：验证两个客户端订阅同一 session 时都能收到该 session 的广播
# 设计：使用两个独立 writer 订阅相同 scope，断言一次事件分别写入两个连接
async def test_scope_session_allows_multiple_subscribers() -> None:
    broadcaster = IpcEventBroadcaster()
    writer_a = _make_writer()
    writer_b = _make_writer()
    broadcaster.subscribe(writer_a, topics=["run.*"], scope="session:shared")
    broadcaster.subscribe(writer_b, topics=["run.*"], scope="session:shared")

    await broadcaster.handle(_run_started("run-shared", "shared"))

    writer_a.write.assert_called_once()  # type: ignore[attr-defined]
    writer_b.write.assert_called_once()  # type: ignore[attr-defined]


# 功能：验证同一连接切换 session 订阅后不再接收旧 session 的事件
# 设计：对同一 writer 连续 subscribe 两次，断言 broadcaster 只保留最后一次 scope
async def test_subscribe_replaces_previous_scope_for_same_writer() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"], scope="session:old")
    broadcaster.subscribe(writer, topics=["run.*"], scope="session:new")

    await broadcaster.handle(_run_started("run-old", "old"))
    await broadcaster.handle(_run_started("run-new", "new"))

    writer.write.assert_called_once()  # type: ignore[attr-defined]


# 功能：验证 unsubscribe 后 handle 不再向该 writer 发送事件
# 设计：先 subscribe 再 unsubscribe，再调用 handle，断言 write 从未被调用，验证订阅生命周期的正确性
async def test_unsubscribe_stops_delivery() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"])
    broadcaster.unsubscribe(writer)

    await broadcaster.handle(_run_started())

    writer.write.assert_not_called()  # type: ignore[attr-defined]


# 功能：验证写入失败（ConnectionResetError）后订阅自动移除，下次 handle 不再尝试写入
# 设计：drain() 抛出 ConnectionResetError 触发死连接清理；断言第二次 handle 时 write 未被调用；
#       第一次 write 在 drain 前已执行，call_count==1 是预期行为而非被测点
async def test_dead_connection_removed_after_failure() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer(drain_raises=ConnectionResetError())
    broadcaster.subscribe(writer, topics=["run.*"])

    event = _run_started()
    await broadcaster.handle(event)  # drain fails → subscription removed

    assert writer.write.call_count == 1  # type: ignore[attr-defined]

    writer.write.reset_mock()  # type: ignore[attr-defined]
    await broadcaster.handle(event)  # no subscribers remain
    writer.write.assert_not_called()  # type: ignore[attr-defined]
