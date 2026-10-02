from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agent_lite.core.app import CoreApp, _RunningRun
from agent_lite.core.bus.envelope import HandlerError
from agent_lite.core.config import McpServerConfig
from agent_lite.core.mcp.client import McpClient, McpToolDef
from agent_lite.core.mcp.http import McpHttpTransport
from agent_lite.core.mcp.server import McpServerManager
from agent_lite.core.mcp.settings import load_settings, parse_server, save_settings


@pytest.mark.parametrize("entry", [
    {"name": "bad name", "command": "python"},
    {"name": "demo", "command": "python", "enabled": "false"},
    {"name": "demo", "command": "python", "args": "server.py"},
    {"name": "demo", "transport": "http", "url": "file:///secret"},
    {"name": "demo", "transport": "http", "url": "https://a:b@example.com/mcp"},
    {"name": "demo", "command": "python", "bearer_token_env": "actual-token!"},
    {"name": "demo", "command": "python", "startup_timeout": float("nan")},
])
def test_invalid_config_does_not_replace_connections(entry: object) -> None:
    with pytest.raises(ValueError):
        parse_server(entry)


async def test_persistence_disable_reload_and_invalid_file_preserve_live_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[AsyncMock] = []

    def client_factory() -> AsyncMock:
        client = AsyncMock(spec=McpClient)
        client.is_connected = True
        client.list_tools.return_value = [McpToolDef("echo", "echo text", {"type": "object"})]
        clients.append(client)
        return client

    monkeypatch.setattr("agent_lite.core.mcp.server.McpClient", client_factory)
    path = tmp_path / "mcp.json"
    cfg = McpServerConfig("demo", command="python", env={"SECRET": "private-value"})
    manager = McpServerManager(path)
    await manager.start_all([cfg])
    assert manager.get_tools()[0].name == "demo__echo"
    assert "private-value" not in json.dumps(manager.snapshot())
    await manager.set_enabled("demo", False)
    assert not manager.get_tools()
    clients[0].close.assert_awaited_once()
    assert load_settings(path, [cfg])[0].enabled is False
    await manager.set_enabled("demo", True)
    assert len(manager.get_tools()) == 1
    await manager.reload()
    assert len(clients) == 2  # 未改动的服务器不重启、不重复注册工具。
    clients[-1].is_connected = False
    assert manager.snapshot()["servers"][0]["status"] == "error"
    assert not manager.get_tools()
    await manager.reload()
    assert len(clients) == 3
    path.write_text('{"servers": "invalid"}', encoding="utf8")
    with pytest.raises(ValueError):
        await manager.reload()
    assert len(manager.get_tools()) == 1
    await manager.stop_all()
    assert not manager.get_tools()


async def test_failed_discovery_cleans_child_and_reports_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock(spec=McpClient)
    client.list_tools.side_effect = ValueError("failed with private-value")
    monkeypatch.setattr("agent_lite.core.mcp.server.McpClient", lambda: client)
    manager = McpServerManager(tmp_path / "mcp.json")
    await manager.start_all([
        McpServerConfig("demo", command="python", env={"SECRET": "private-value"}),
        McpServerConfig("disabled", command="python", enabled=False),
    ])
    assert not manager.get_tools()
    client.close.assert_awaited_once()
    snapshot = manager.snapshot()
    assert snapshot["servers"][0]["status"] == "error"
    assert "private-value" not in json.dumps(snapshot)
    assert snapshot["servers"][1]["status"] == "disabled"


async def test_corrupt_startup_config_keeps_chat_and_config_editor_available(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text("broken JSON", encoding="utf8")
    manager = McpServerManager(path)
    await manager.start_all([])
    assert manager.snapshot()["configError"]
    assert manager.prepare_settings() == path
    assert path.read_text() == "broken JSON"


async def test_core_blocks_mutations_and_new_runs_during_management(tmp_path: Path) -> None:
    app = CoreApp()
    app._mcp_manager = McpServerManager(tmp_path / "mcp.json")
    task = asyncio.create_task(asyncio.sleep(100, result="done"))
    app._running_runs["run"] = _RunningRun("session", task)
    try:
        assert (await app._mcp_list_handler({}))["servers"] == []
        with pytest.raises(HandlerError, match="任务"):
            await app._mcp_manage_handler({"action": "reload"})
        assert (await app._mcp_manage_handler({"action": "configure"}))["settingsPath"]
        app._mcp_changing = True
        with pytest.raises(HandlerError, match="MCP"):
            app._start_session_run("session", "hello", "run2")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_real_stdio_tools_call_and_reconnect(tmp_path: Path) -> None:
    script = tmp_path / "server.py"
    script.write_text('''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo text", "inputSchema": {"type": "object"}}]}
    else:
        result = {"content": [{"type": "text", "text": request["params"]["arguments"]["text"]}]}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''', encoding="utf8")
    manager = McpServerManager(tmp_path / "mcp.json")
    try:
        await manager.start_all([McpServerConfig("echo", command=sys.executable, args=[str(script)])])
        assert manager.snapshot()["servers"][0]["status"] == "connected"
        answer = await manager.get_tools()[0].invoke({"text": "真实 MCP 调用"})
        assert answer.content == "真实 MCP 调用"
        await manager.reconnect("echo")
        assert len(manager.get_tools()) == 1
        await manager.set_enabled("echo", False)
        assert not manager.get_tools()
        restored = McpServerManager(tmp_path / "mcp.json")
        await restored.start_all([])
        assert restored.snapshot()["servers"][0]["status"] == "disabled"
        await restored.stop_all()
    finally:
        await manager.stop_all()


async def test_http_json_sse_headers_notifications_and_session_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_MCP_TOKEN", "test-token")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.method == "DELETE":
            assert request.headers["Mcp-Session-Id"] == "test-session"
            return httpx.Response(204)
        message = json.loads(request.content)
        assert request.headers["Accept"] == "application/json, text/event-stream"
        if message["method"] == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "test-session"}, json={
                "jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2025-03-26"},
            })
        assert request.headers["Mcp-Session-Id"] == "test-session"
        assert request.headers["MCP-Protocol-Version"] == "2025-03-26"
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "tools/list":
            if message["params"].get("cursor"):
                result: dict[str, object] = {"tools": []}
            else:
                result = {"tools": [{"name": "echo", "description": "Echo"}], "nextCursor": "page2"}
        else:
            result = {"content": [{"type": "text", "text": "echo from HTTP"}]}
        payload = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              text='data: {"method":"notifications/progress"}\n\n'
                              + "data: " + payload + "\n\n")

    client = McpClient()
    http = McpHttpTransport("https://example.test/mcp", "TEST_MCP_TOKEN")
    await http._client.aclose()
    http._client = httpx.AsyncClient(transport=httpx.MockTransport(handle), headers={
        "Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream",
    })
    client._http = http
    try:
        await client._initialize()
        assert client.is_connected
        tools = await client.list_tools()
        assert len(tools) == 1
        assert await client.call_tool("echo", {}) == "echo from HTTP"
    finally:
        await client.close()
    assert requests[-1].method == "DELETE"


async def test_http_auth_failure_reports_action_without_exposing_response() -> None:
    http = McpHttpTransport("https://example.test/mcp")
    await http._client.aclose()
    http._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, text="secret-token-in-error")
    ))
    try:
        with pytest.raises(ValueError, match="OAuth") as error:
            await http.post({"id": 1})
        assert "secret-token" not in str(error.value)
    finally:
        await http.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows npm wrapper")
async def test_windows_npx_uses_node_launcher_with_exact_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shim = tmp_path / "npx.cmd"
    cli = tmp_path / "node_modules/npm/bin/npx-cli.js"
    cli.parent.mkdir(parents=True)
    cli.touch()
    node = tmp_path / "node.exe"
    node.touch()
    monkeypatch.setattr("agent_lite.core.mcp.client.shutil.which", lambda cmd, **kw: str(shim))
    process = SimpleNamespace(stdout=None, stdin=None, stderr=None, returncode=None,
                              terminate=Mock(), wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("agent_lite.core.mcp.client.asyncio.create_subprocess_exec", spawn)
    client = McpClient()
    monkeypatch.setattr(client, "_initialize", AsyncMock())
    await client.connect_stdio("npx", ["-y", "package", "C:/path with spaces"])
    assert spawn.call_args.args == (str(node), str(cli), "-y", "package", "C:/path with spaces")
    await client.close()


def test_settings_write_and_duplicate_names(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    cfg = McpServerConfig("example", command="python")
    save_settings(path, [cfg])
    assert load_settings(path, []) == [cfg]
    save_settings(path, [cfg, cfg])
    with pytest.raises(ValueError, match="重复"):
        load_settings(path, [])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows npm wrapper")
async def test_windows_scoped_npm_bin_preserves_node_flags_and_literal_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shim = tmp_path / "zg.cmd"
    cli = tmp_path / "node_modules/@zvec/zvec-grep/dist/cli/index.js"
    cli.parent.mkdir(parents=True)
    cli.touch()
    shim.write_text('"%_prog%" --liftoff-only "%dp0%\\node_modules\\@zvec\\zvec-grep\\dist\\cli\\index.js" %*', encoding="utf-8")
    node = tmp_path / "node.exe"
    node.touch()
    monkeypatch.setattr("agent_lite.core.mcp.client.shutil.which", lambda cmd, **kw: str(shim))
    process = SimpleNamespace(stdout=None, stdin=None, stderr=None, returncode=None,
                              terminate=Mock(), wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("agent_lite.core.mcp.client.asyncio.create_subprocess_exec", spawn)
    client = McpClient()
    monkeypatch.setattr(client, "_initialize", AsyncMock())
    args = ["mcp", "C:/path with spaces", "a&b", "%TEMP%"]
    await client.connect_stdio(str(shim), args)
    assert spawn.call_args.args == (str(node), "--liftoff-only", str(cli), *args)
    assert spawn.call_args.kwargs["creationflags"] != 0
    await client.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows npm wrapper")
async def test_windows_npm_shim_cannot_escape_package_directory(
    tmp_path: Path,
) -> None:
    shim = tmp_path / "custom.cmd"
    (tmp_path / "outside.js").touch()
    shim.write_text('"%_prog%" "%dp0%\\node_modules\\..\\outside.js" %*', encoding="utf-8")
    with pytest.raises(ValueError, match="npm"):
        await McpClient().connect_stdio(str(shim), [])
