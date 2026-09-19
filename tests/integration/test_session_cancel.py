from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from agent_lite.core.transport.socket_client import SocketClient


# 功能：通过真实 TCP 连接取消当前 run，但不关闭 Session 或 Core
# 设计：等到 run.started 后发 session.cancel，再验证取消事件、Session ready 和后续 ping
async def test_cancel_run_keeps_session_and_core_alive(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
) -> None:
    client = SocketClient("127.0.0.1", free_port)
    await client.connect()
    started = asyncio.Event()
    finished = asyncio.Event()
    ready = asyncio.Event()
    observed: dict[str, dict[str, Any]] = {}

    async def on_event(event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "run.started":
            observed["started"] = event
            started.set()
        elif event_type == "run.finished" and event.get("reason") == "cancelled":
            observed["finished"] = event
            finished.set()
        elif event_type == "session.waiting_for_input":
            observed["ready"] = event
            ready.set()

    client.on_event(on_event)
    loop_task = asyncio.create_task(client.run_event_loop())
    try:
        created = await client.send_command("session.create", {"mode": "chat"})
        session_id = str(created["session_id"])
        await client.send_command(
            "event.subscribe",
            {"topics": ["run.*", "session.*"], "scope": f"session:{session_id}"},
        )
        send_task = asyncio.create_task(
            client.send_command(
                "session.send_message",
                {"session_id": session_id, "content": "keep working until cancelled"},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        run_id = str(observed["started"]["run_id"])

        cancelled = await client.send_command(
            "session.cancel",
            {"session_id": session_id, "run_id": run_id},
        )
        await asyncio.wait_for(
            asyncio.gather(finished.wait(), ready.wait()),
            timeout=5,
        )
        send_result = await asyncio.wait_for(send_task, timeout=5)
        pong = await client.send_command("core.ping", {"client": "cancel-test"})

        assert cancelled == {"run_id": run_id, "accepted": True}
        assert send_result["run_id"] == run_id
        assert observed["ready"]["last_run_id"] == run_id
        assert pong["server_version"]
        assert running_daemon.poll() is None
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        await client.close()
