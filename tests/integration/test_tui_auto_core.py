from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_lite.cli.commands.core import cmd_core_start
from agent_lite.core.config import AgentLiteConfig
from agent_lite.tui import core_connection


# 配置隔离的模型与日志，并捕获测试启动的进程以保证回收
@pytest.fixture
def isolated_core_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[subprocess.Popen[bytes]]]:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[llm]\nprotocol="openai"\ndefault_model="test"\nbase_url="http://127.0.0.1:1"\n'
        '[trace]\nenabled=false\n[web]\nenabled=false\n'
        f'[session]\ndir="{tmp_path.as_posix()}/sessions"\n'
        f'[memory]\ndir="{tmp_path.as_posix()}/memory.db"\ngenerate_enabled=false\n'
        f'[logging]\nfile="{tmp_path.as_posix()}/core.log"\n', encoding="utf-8",
    )
    monkeypatch.setenv("AGENTLITE_CONFIG", str(config_path))
    monkeypatch.setenv("AGENTLITE_LOG_FILE", str(tmp_path / "core.log"))
    monkeypatch.setenv("LLM_API_KEY", "local-test-only")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    processes: list[subprocess.Popen[bytes]] = []
    original = subprocess.Popen

    # 捕获本测试启动的进程，确保失败时也不会留下测试 core
    def capture(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = original(*args, **kwargs)  # type: ignore[arg-type]
        processes.append(proc)
        return proc

    monkeypatch.setattr(core_connection.subprocess, "Popen", capture)
    yield processes
    for proc in processes:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# 功能：TUI 无需手动启动 core，与另一前端共享，全部退出后自动停止
# 设计：调用实际 TUI 连接器启动真实无窗口 Python core，仅使用本地假凭证而不请求模型
@pytest.mark.asyncio
async def test_tui_autostart_and_shared_exit(
    free_port: int, tmp_path: Path, isolated_core_processes: list[subprocess.Popen[bytes]],
) -> None:
    processes = isolated_core_processes
    client = None
    reader = None
    second = None
    second_reader = None
    try:
        client = await core_connection.connect_frontend("127.0.0.1", free_port, str(tmp_path))
        reader = asyncio.create_task(client.run_event_loop())
        lease = await client.send_command("frontend.register", {"client": "tui"})
        assert lease["managed"] is True
        assert lease["frontend_counts"] == {"vscode": 0, "tui": 1}
        second = await core_connection.connect_frontend("127.0.0.1", free_port, str(tmp_path))
        second_reader = asyncio.create_task(second.run_event_loop())
        lease = await second.send_command("frontend.register", {"client": "vscode"})
        assert lease["frontend_counts"] == {"vscode": 1, "tui": 1}
        assert len(processes) == 1
        await client.send_command("frontend.unregister", {})
        await client.close()
        lease = await second.send_command("frontend.heartbeat", {})
        assert lease["frontend_counts"] == {"vscode": 1, "tui": 0}
        pong = await second.send_command("core.ping", {"client": "remaining-frontend"})
        assert pong["server_version"]
        await second.close()
        returncode = await asyncio.to_thread(processes[0].wait, 25)
        assert returncode == 0
    finally:
        if client is not None:
            await client.close()
        if second is not None:
            await second.close()
        await asyncio.gather(
            *([reader] if reader is not None else []),
            *([second_reader] if second_reader is not None else []),
            return_exceptions=True,
        )


# 功能：手动启动复用已有自动 core 并提升为常驻，主动停止仍然正常退出
# 设计：运行真正的 CLI 管理函数和 TCP 协议，确认没有启动第二个 core
@pytest.mark.asyncio
async def test_manual_start_promotes_existing_core(
    free_port: int, tmp_path: Path, isolated_core_processes: list[subprocess.Popen[bytes]],
) -> None:
    client = await core_connection.connect_frontend("127.0.0.1", free_port, str(tmp_path))
    reader = asyncio.create_task(client.run_event_loop())
    try:
        lease = await client.send_command("frontend.register", {"client": "tui"})
        assert lease["managed"] is True
        await asyncio.to_thread(cmd_core_start, AgentLiteConfig(port=free_port))
        lease = await client.send_command("frontend.heartbeat", {})
        assert lease["managed"] is False
        assert len(isolated_core_processes) == 1
        await client.send_command("frontend.unregister", {})
        assert (await client.send_command("core.ping", {"client": "observer"}))["server_version"]
        await client.send_command("core.shutdown", {})
        assert await asyncio.to_thread(isolated_core_processes[0].wait, 5) == 0
    finally:
        await client.close()
        await asyncio.gather(reader, return_exceptions=True)
