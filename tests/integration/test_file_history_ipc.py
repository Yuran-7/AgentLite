from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent_lite.core.session.ids import new_session_id
from agent_lite.core.session.model import Session
from agent_lite.core.session.store import SessionStore
from agent_lite.core.tools.file_operations import FileOperationService


# 发送一次文件历史协议请求并取得响应
async def request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    action: str,
    params: dict[str, object],
) -> dict:
    writer.write(
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": action,
                    "method": f"file_history.{action}",
                    "params": params,
                }
            )
            + "\n"
        ).encode()
    )
    await writer.drain()
    return json.loads(await asyncio.wait_for(reader.readline(), 10))


# 功能：验证真实守护进程暴露历史列表、差异、恢复及冲突校验
# 设计：构造磁盘会话后直接访问历史，不调用模型或改变聊天恢复状态
async def test_file_history_rpc_and_cli(
    running_daemon: subprocess.Popen[bytes], free_port: int, tmp_path: Path
) -> None:
    sid = new_session_id()
    store = SessionStore(tmp_path / "sessions")
    now = datetime.now(UTC).isoformat()
    session = Session(
        id=sid,
        mode="chat",
        status="active",
        title="history test",
        created_at=now,
        updated_at=now,
        workspace_root=str(tmp_path),
    )
    store.write_meta(session)
    files = FileOperationService(tmp_path, store.session_dir(sid))
    files.metadata["run_id"] = "run-history"
    files.write("file.txt", "before")
    files.write("file.txt", "after")
    assert files.history is not None
    change = files.history.records()[-1]["id"]
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
    try:
        listed = await request(reader, writer, "list", {"session_id": sid})
        assert len(listed["result"]["changes"]) == 2
        params = {"session_id": sid, "change_id": change}
        diff = await request(reader, writer, "diff", params)
        assert "-before" in diff["result"]["diff"] and "+after" in diff["result"]["diff"]
        assert "\n-before\n" in diff["result"]["diff"]
        assert "\n+after\n" in diff["result"]["diff"]
        first = listed["result"]["changes"][0]["id"]
        combined = await request(reader, writer, "diff", {**params, "from_change_id": first})
        assert "+after" in combined["result"]["diff"]
        assert "-before" not in combined["result"]["diff"]
        invalid = await request(reader, writer, "diff", {
            "session_id": sid, "change_id": first, "from_change_id": change,
        })
        assert invalid["error"]["code"] == -32030
        preview = await request(reader, writer, "restore", {**params, "dry_run": True})
        assert preview["result"]["dry_run"]
        assert (tmp_path / "file.txt").read_bytes() == b"after"
        restored = await request(reader, writer, "restore", params)
        assert restored["result"]["change_id"]
        assert (tmp_path / "file.txt").read_bytes() == b"before"
        updated = await request(reader, writer, "list", {"session_id": sid})
        assert updated["result"]["changes"][-1]["restores"] == change
        conflict = await request(reader, writer, "restore", params)
        assert conflict["error"]["code"] == -32030
    finally:
        writer.close()
        await writer.wait_closed()
    env = dict(os.environ, AGENTLITE_PORT=str(free_port), AGENTLITE_LOG_FILE="")
    cli = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from agent_lite.cli.main import main; main()",
        "history",
        "list",
        "--session",
        sid,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(cli.communicate(), 15)
    assert cli.returncode == 0, stderr.decode(errors="replace")
    assert len(json.loads(stdout)["changes"]) == 3
