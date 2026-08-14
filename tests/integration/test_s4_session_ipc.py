from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path


# 发送一条 JSON-RPC 请求并返回响应对象
async def _send_recv(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    params: dict,
    req_id: str = "1",
) -> dict:
    req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    return json.loads(line)


# 功能：验证 daemon 暴露 session.create、session.get_history、session.close 三个 S4 IPC 命令
# 设计：不触发 session.send_message，避免真实 LLM 依赖；只验证 CoreApp handler 注册、协议序列化和 session 状态持久化
async def test_session_create_history_close_over_ipc(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
    tmp_path: Path,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)

    created = await _send_recv(
        reader,
        writer,
        "session.create",
        {
            "mode": "chat",
            "title": "ipc test",
            "workspace_root": str(tmp_path),
        },
        req_id="create",
    )
    assert "result" in created, created
    session_id = created["result"]["session_id"]
    assert created["result"]["status"] == "active"
    assert created["result"]["workspace_root"] == str(tmp_path.resolve())

    history = await _send_recv(
        reader,
        writer,
        "session.get_history",
        {"session_id": session_id},
        req_id="history",
    )
    assert history["result"]["messages"] == []

    closed = await _send_recv(
        reader,
        writer,
        "session.close",
        {"session_id": session_id},
        req_id="close",
    )
    assert closed["result"]["status"] == "closed"

    writer.close()
    await writer.wait_closed()


# 功能：验证 TUI 可通过 IPC 为已创建的未绑定 session 首次补充工作区
# 设计：对真实 daemon 依次调用 create 与 set_workspace，确认新命令已注册且返回规范化目录
async def test_session_set_workspace_over_ipc(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
    tmp_path: Path,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
    created = await _send_recv(
        reader,
        writer,
        "session.create",
        {"mode": "chat"},
        req_id="create-unbound",
    )
    session_id = created["result"]["session_id"]
    assert created["result"]["workspace_root"] is None

    attached = await _send_recv(
        reader,
        writer,
        "session.set_workspace",
        {"session_id": session_id, "workspace_root": str(tmp_path)},
        req_id="set-workspace",
    )

    assert attached["result"]["workspace_root"] == str(tmp_path.resolve())
    writer.close()
    await writer.wait_closed()
