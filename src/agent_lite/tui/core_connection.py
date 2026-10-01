from __future__ import annotations

import asyncio
import errno
import os
import subprocess
import sys
from pathlib import Path

from agent_lite.core.transport.socket_client import SocketClient


# 启动前端托管的无窗口 core，日志独立于 TUI 终端并保留工作区配置
def launch_frontend_core(host: str, port: int, workspace: str | None) -> None:
    log_path = Path.home() / ".agentlite" / "logs" / "frontend-launch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as log:
        subprocess.Popen(
            [sys.executable, "-m", "agent_lite.core"],
            cwd=workspace or Path.cwd(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            creationflags=(
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt" else 0
            ),
            start_new_session=os.name != "nt",
            env={**os.environ, "AGENTLITE_HOST": host, "AGENTLITE_PORT": str(port),
                 "AGENTLITE_FRONTEND_MANAGED": "1"},
        )


# 优先连接已有 core，只有本机端口拒绝连接时才自动启动并等待就绪
async def connect_frontend(host: str, port: int, workspace: str | None) -> SocketClient:
    client = SocketClient(host, port)
    try:
        await client.connect()
        return client
    except OSError as exc:
        refused = isinstance(exc, ConnectionRefusedError) or exc.errno == errno.ECONNREFUSED
        if host not in {"127.0.0.1", "localhost", "::1"} or not refused:
            raise
    await asyncio.to_thread(launch_frontend_core, host, port, workspace)
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        try:
            await client.connect()
            return client
        except OSError as exc:
            if not isinstance(exc, ConnectionRefusedError) and exc.errno != errno.ECONNREFUSED:
                raise
        await asyncio.sleep(0.15)
    raise OSError("core 未在 30 秒内启动，请查看 ~/.agentlite/logs/frontend-launch.log")


# 周期续约前端租约，失败后关闭连接让 TUI 进入重连流程
async def frontend_heartbeat(client: SocketClient, interval: float) -> None:
    try:
        while True:
            await asyncio.sleep(interval)
            await asyncio.wait_for(client.send_command("frontend.heartbeat", {}), timeout=5)
    except (OSError, RuntimeError, TimeoutError):
        await client.close()
