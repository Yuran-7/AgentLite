from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent_lite.core.transport.socket_client import IpcError, SocketClient


async def _start_mock_server(
    handler: Any,
) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port: int = server.sockets[0].getsockname()[1]
    return server, port


# 功能：验证 send_command 向 mock server 发送 JSON-RPC 请求并正确解析响应 result
# 设计：用 asyncio.start_server + port 0 启动内存中的 mock server，避免依赖真实 daemon；
#       loop_task 并发运行 run_event_loop，使 send_command 的 future 能被 _dispatch 解析
async def test_send_command_returns_result() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        req = json.loads(line)
        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"pong": True}}
        writer.write(json.dumps(resp).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)
        await client.connect()
        loop_task = asyncio.create_task(client.run_event_loop())

        result = await asyncio.wait_for(
            client.send_command("core.ping", {"client": "test"}),
            timeout=2.0,
        )
        assert result == {"pong": True}

        await loop_task
        await client.close()


# 功能：验证 server 返回 JSON-RPC error 时 send_command 抛出 IpcError 并携带正确错误码
# 设计：mock server 返回 error 对象（code=-32601），断言异常类型和 code 属性，确认客户端的错误路径处理
async def test_send_command_raises_ipc_error() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        req = json.loads(line)
        resp = {
            "jsonrpc": "2.0",
            "id": req["id"],
            "error": {"code": -32601, "message": "Method not found"},
        }
        writer.write(json.dumps(resp).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)
        await client.connect()
        loop_task = asyncio.create_task(client.run_event_loop())

        with pytest.raises(IpcError) as exc_info:
            await asyncio.wait_for(
                client.send_command("core.nope", {}),
                timeout=2.0,
            )
        assert exc_info.value.code == -32601

        await loop_task
        await client.close()


# 功能：验证 Response 与 event.push Notification 在同一连接交错到达时能正确分发
# 设计：server 先推送无 id 的 Notification，再返回带 id 的 Response，分别验证 handler 和 Future
async def test_event_push_routed_to_handler() -> None:
    received_events: list[dict[str, Any]] = []
    push_done = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        req = json.loads(line)
        push = {
            "jsonrpc": "2.0",
            "method": "event.push",
            "params": {"type": "run.started", "run_id": "r1", "session_id": "s1"},
        }
        writer.write(json.dumps(push).encode() + b"\n")
        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"subscription_id": "sub-1"}}
        writer.write(json.dumps(resp).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)

        async def collect(event_data: dict[str, Any]) -> None:
            received_events.append(event_data)
            push_done.set()

        client.on_event(collect)
        await client.connect()
        loop_task = asyncio.create_task(client.run_event_loop())

        await asyncio.wait_for(
            client.send_command("event.subscribe", {"topics": ["run.*"]}),
            timeout=2.0,
        )
        await asyncio.wait_for(push_done.wait(), timeout=2.0)

        assert len(received_events) == 1
        assert received_events[0] == {
            "type": "run.started",
            "run_id": "r1",
            "session_id": "s1",
        }

        await loop_task
        await client.close()


# 功能：验证 Notification 只走事件 handler，不会创建或完成 pending Future
# 设计：预先放入一个 pending Future，直接分发无 id 通知后检查它仍未完成
async def test_notification_does_not_touch_pending_future() -> None:
    client = SocketClient("127.0.0.1", 1)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    client._pending["request-1"] = future  # type: ignore[attr-defined]
    received: list[dict[str, Any]] = []

    async def collect(event_data: dict[str, Any]) -> None:
        received.append(event_data)

    client.on_event(collect)
    await client._dispatch(  # type: ignore[attr-defined]
        b'{"jsonrpc":"2.0","method":"event.push","params":{"type":"llm.token"}}\n'
    )

    assert received == [{"type": "llm.token"}]
    assert client._pending == {"request-1": future}  # type: ignore[attr-defined]
    assert not future.done()
    future.cancel()


# 功能：未知 Notification 只记录警告，并且不影响后续合法 event.push
# 设计：连续分发未知和已知通知，检查日志及 handler 最终只收到合法事件
async def test_unknown_notification_is_logged_and_next_message_is_processed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = SocketClient("127.0.0.1", 1)
    received: list[dict[str, Any]] = []

    async def collect(event_data: dict[str, Any]) -> None:
        received.append(event_data)

    client.on_event(collect)
    with caplog.at_level(
        "WARNING", logger="agent_lite.core.transport.socket_client"
    ):
        await client._dispatch(  # type: ignore[attr-defined]
            b'{"jsonrpc":"2.0","method":"server.unknown","params":{}}\n'
        )
        await client._dispatch(  # type: ignore[attr-defined]
            b'{"jsonrpc":"2.0","method":"event.push","params":{"type":"run.finished"}}\n'
        )

    assert "unknown notification method: server.unknown" in caplog.text
    assert received == [{"type": "run.finished"}]


# 功能：验证错误版本、非对象及非对象 params 都被丢弃，缺省 params 则视为空对象
# 设计：集中覆盖协议边界，断言只有缺省 params 的合法 Notification 触发 handler
async def test_invalid_notifications_are_ignored_and_missing_params_is_empty_object() -> None:
    client = SocketClient("127.0.0.1", 1)
    received: list[dict[str, Any]] = []

    async def collect(event_data: dict[str, Any]) -> None:
        received.append(event_data)

    client.on_event(collect)
    for frame in (
        b'[]\n',
        b'{"jsonrpc":"1.0","method":"event.push","params":{}}\n',
        b'{"jsonrpc":"2.0","method":"event.push","params":[]}\n',
        b'{"jsonrpc":"2.0","method":"event.push"}\n',
    ):
        await client._dispatch(frame)  # type: ignore[attr-defined]

    assert received == [{}]


# 功能：验证 server 关闭连接后 run_event_loop 正常退出（不挂起）
# 设计：mock server 立即关闭写流，client readline 收到空字节后退出循环；
#       用 asyncio.wait_for 设置超时，防止 loop 意外挂起导致测试卡死
async def test_run_event_loop_exits_on_server_close() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)
        await client.connect()
        await asyncio.wait_for(client.run_event_loop(), timeout=2.0)
        await client.close()
