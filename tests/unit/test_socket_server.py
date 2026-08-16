from __future__ import annotations

import asyncio
import socket

from agent_lite.core.transport.socket_server import SocketServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# 功能：验证客户端断开后 SocketServer 调用 broadcaster.unsubscribe(writer) 清理订阅
# 设计：用内联 MockBroadcaster 捕获 unsubscribe 调用并设置 asyncio.Event，避免 sleep 轮询；
#       等待 Event 而非断言调用次数，确保时序正确性而不依赖竞态假设
async def test_broadcaster_unsubscribe_called_on_disconnect() -> None:
    unsubscribed = asyncio.Event()

    class MockBroadcaster:
        def unsubscribe(self, writer: object) -> None:
            unsubscribed.set()

    port = _free_port()
    server = SocketServer("127.0.0.1", port, broadcaster=MockBroadcaster())  # type: ignore[arg-type]
    await server.start()

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()

        await asyncio.wait_for(unsubscribed.wait(), timeout=2.0)
    finally:
        await server.stop()


# 功能：验证不传入 broadcaster 时 SocketServer 仍可正常启动和停止（backward-compatible 默认值）
# 设计：直接实例化 SocketServer(host, port)（无 broadcaster），start/stop 不抛异常即为通过；
#       回归测试确保新参数的默认值 None 不破坏现有调用方
async def test_no_broadcaster_server_starts_and_stops() -> None:
    port = _free_port()
    server = SocketServer("127.0.0.1", port)
    await server.start()
    await server.stop()


# 功能：验证客户端直接关闭连接时 SocketServer 不向事件循环抛出断连异常
# 设计：让 reader 模拟 Windows Proactor 报出的 ConnectionResetError，断言连接处理函数正常收尾
async def test_client_reset_is_handled_without_raising() -> None:
    class ResetReader:
        async def readline(self) -> bytes:
            raise ConnectionResetError(64, "connection reset")

    class FakeWriter:
        def get_extra_info(self, name: str, default: object = None) -> object:
            return default

        def close(self) -> None:
            return None

    server = SocketServer("127.0.0.1", 0)

    await server._handle_connection(ResetReader(), FakeWriter())  # type: ignore[arg-type]
