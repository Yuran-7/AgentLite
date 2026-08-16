from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from agent_lite.cli.commands.core import _shutdown_request
from agent_lite.core.config import KamaConfig


# 功能：验证 daemon 能通过 core.shutdown IPC 在 Windows 和 POSIX 上优雅退出
# 设计：使用真实 daemon fixture 发出关闭请求并等待子进程结束，覆盖信号注册和 socket 清理链路
@pytest.mark.asyncio
async def test_shutdown_over_ipc(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
    tmp_path: Path,
) -> None:
    assert (tmp_path / "sessions").is_dir()
    config = KamaConfig(port=free_port)
    await _shutdown_request(config)
    returncode = await asyncio.to_thread(running_daemon.wait, 3)
    assert returncode == 0
