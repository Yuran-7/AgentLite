from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_lite.core.app import CoreApp
from agent_lite.core.config import AgentLiteConfig
from agent_lite.core.mcp.server import McpServerManager
from agent_lite.core.transport.socket_server import SocketServer


async def test_bind_failure_cleans_initialized_mcp_and_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentLiteConfig()
    config.session.dir = str(tmp_path / "sessions")
    config.memory.dir = str(tmp_path / "memory")
    config.trace.enabled = True
    config.trace.file = str(tmp_path / "trace.jsonl")
    monkeypatch.setattr("agent_lite.core.app.get_config", lambda: config)
    monkeypatch.setattr("agent_lite.core.app.setup_logging", lambda _: None)
    start_mcp = AsyncMock()
    stop_mcp = AsyncMock()
    monkeypatch.setattr(McpServerManager, "start_all", start_mcp)
    monkeypatch.setattr(McpServerManager, "stop_all", stop_mcp)
    monkeypatch.setattr(SocketServer, "start", AsyncMock(side_effect=PermissionError(errno.EACCES, "denied")))
    app = CoreApp()
    with pytest.raises(PermissionError):
        await app.run()
    start_mcp.assert_awaited_once()
    stop_mcp.assert_awaited_once()
    assert app._trace is not None and app._trace._task is None
    assert app._server is not None and app._server._server is None


async def test_config_failure_preserves_original_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_config() -> AgentLiteConfig:
        raise ValueError("invalid config")
    monkeypatch.setattr("agent_lite.core.app.get_config", fail_config)
    with pytest.raises(ValueError, match="invalid config"):
        await CoreApp().run()
