from __future__ import annotations

import asyncio
import subprocess

import pytest

from agent_lite.core.transport.socket_client import IpcError, SocketClient


# 功能：真实 daemon 支持创建、读取、更新和恢复权限模式，非法值返回协议错误。
# 设计：用无模型运行的 IPC 往返确认接口与 SessionManager 的完整接线。
async def test_permission_modes_rpc(
    running_daemon: subprocess.Popen[bytes], free_port: int
) -> None:
    client = SocketClient("127.0.0.1", free_port)
    await client.connect()
    loop = asyncio.create_task(client.run_event_loop())
    try:
        created = await client.send_command("session.create", {"mode": "chat"})
        assert created["permission_mode"] == "auto"
        sid = created["session_id"]
        result = await client.send_command(
            "session.permission_mode",
            {
                "session_id": sid,
                "mode": "accept_edits",
            },
        )
        assert result["mode"] == "accept_edits"
        assert (await client.send_command("session.permission_mode", {"session_id": sid}))[
            "mode"
        ] == "accept_edits"
        with pytest.raises(IpcError) as error:
            await client.send_command("session.permission_mode", {"session_id": sid, "mode": "bad"})
        assert error.value.code == -32602
        resumed = await client.send_command("session.resume", {"session_id": sid})
        assert resumed["permission_mode"] == "accept_edits"
        await client.send_command("session.collaboration", {"session_id": sid, "mode": "plan"})
        result = await client.send_command(
            "session.collaboration",
            {
                "session_id": sid,
                "mode": "default",
                "permission_mode": "manual",
            },
        )
        assert result == {"mode": "default", "permission_mode": "manual"}
    finally:
        loop.cancel()
        await client.close()
        await asyncio.gather(loop, return_exceptions=True)
