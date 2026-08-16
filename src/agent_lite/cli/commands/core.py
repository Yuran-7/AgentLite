from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from agent_lite.core.config import KamaConfig
from agent_lite.core.transport.socket_client import SocketClient

_PID_FILE = Path.home() / ".kama" / "kama-core.pid"


# 尝试连接 daemon，成功则正常返回，失败则抛出 ConnectionRefusedError/OSError
async def _ping_check(config: KamaConfig) -> None:
    _r, w = await asyncio.open_connection(config.host, config.port)
    w.close()
    await w.wait_closed()


# 通过 TCP IPC 请求 daemon 优雅退出，并等待 JSON-RPC 响应
async def _shutdown_request(config: KamaConfig) -> None:
    client = SocketClient(config.host, config.port)
    await client.connect()
    event_loop = asyncio.create_task(client.run_event_loop())
    try:
        await asyncio.wait_for(
            client.send_command("core.shutdown", {"type": "core.shutdown"}),
            timeout=2.0,
        )
    finally:
        await client.close()
        await event_loop


# 打印 daemon 当前状态（running / not running）
def cmd_core_status(config: KamaConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"running  ({config.host}:{config.port})")
    except (ConnectionRefusedError, OSError):
        print("not running")


# 在后台启动 daemon，若已在运行则提示并退出
def cmd_core_start(config: KamaConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"already running  ({config.host}:{config.port})")
        return
    except (ConnectionRefusedError, OSError):
        pass

    if os.name == "nt":
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_lite.core"],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_lite.core"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(proc.pid))
    print(f"started  pid={proc.pid}  ({config.host}:{config.port})")


# 通过跨平台 IPC 请求 daemon 优雅停止，若未运行则清理过期 PID 文件
def cmd_core_stop(config: KamaConfig) -> None:
    pid = _PID_FILE.read_text().strip() if _PID_FILE.exists() else "unknown"
    try:
        asyncio.run(_shutdown_request(config))
    except (ConnectionRefusedError, OSError, TimeoutError, asyncio.CancelledError):
        _PID_FILE.unlink(missing_ok=True)
        print("not running")
        return
    _PID_FILE.unlink(missing_ok=True)
    print(f"stopped  pid={pid}")
