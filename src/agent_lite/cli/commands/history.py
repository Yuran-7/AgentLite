from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress

from agent_lite.core.config import AgentLiteConfig
from agent_lite.core.transport.socket_client import IpcError, SocketClient


# 通过核心协议查看或恢复文件历史，连接或冲突失败时返回非零退出码
def cmd_history(
    config: AgentLiteConfig,
    action: str,
    session_id: str,
    change_id: str | None = None,
    dry_run: bool = False,
) -> None:
    try:
        asyncio.run(_history(config, action, session_id, change_id, dry_run))
    except (OSError, IpcError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


# 连接守护进程并维持响应读取，完成后关闭连接及事件任务
async def _history(
    config: AgentLiteConfig, action: str, session_id: str, change_id: str | None, dry_run: bool
) -> None:
    client = SocketClient(config.host, config.port)
    await client.connect()
    reader = asyncio.create_task(client.run_event_loop())
    params: dict[str, object] = {"session_id": session_id}
    if change_id is not None:
        params["change_id"] = change_id
    if action == "restore":
        params["dry_run"] = dry_run
    try:
        result = await asyncio.wait_for(
            client.send_command(f"file_history.{action}", params), timeout=120
        )
        if action == "diff":
            print(result["diff"])
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await client.close()
        reader.cancel()
        with suppress(asyncio.CancelledError):
            await reader
