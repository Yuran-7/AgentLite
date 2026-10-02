from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_lite.core.config import McpServerConfig
from agent_lite.core.mcp.client import McpClient
from agent_lite.core.mcp.settings import load_settings, save_settings, settings_path
from agent_lite.core.mcp.tool import McpTool
from agent_lite.core.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


# 管理所有 MCP server 连接的生命周期：启动、工具发现、注册、关闭
class McpServerManager:
    def __init__(self, path: Path | None = None) -> None:
        self.settings_path = path or settings_path()
        self._clients: dict[str, McpClient] = {}
        self._tools: list[McpTool] = []
        self._configs: dict[str, McpServerConfig] = {}
        self._fallback: list[McpServerConfig] = []
        self._errors: dict[str, str] = {}
        self.config_error = ""

    # 依次连接每个 MCP server，发现工具后缓存供后续 registry 使用；失败时记录日志并跳过
    async def start_all(self, servers: list[McpServerConfig]) -> None:
        self._fallback = list(servers)
        try:
            await self.reload()
        except ValueError as exc:
            self.config_error = str(exc)
            await self._apply(servers)

    async def reload(self) -> None:
        configs = load_settings(self.settings_path, self._fallback)
        await self._apply(configs)
        self.config_error = ""

    def prepare_settings(self) -> Path:
        if not self.settings_path.exists():
            save_settings(self.settings_path, self._fallback)
        return self.settings_path

    async def set_enabled(self, name: str, enabled: bool) -> None:
        configs = load_settings(self.settings_path, self._fallback)
        if not any(cfg.name == name for cfg in configs):
            raise ValueError("MCP 服务器不存在")
        configs = [replace(cfg, enabled=enabled) if cfg.name == name else cfg for cfg in configs]
        save_settings(self.settings_path, configs)
        await self._apply(configs)
        self.config_error = ""

    async def add(self, cfg: McpServerConfig) -> None:
        configs = load_settings(self.settings_path, self._fallback)
        if any(item.name == cfg.name for item in configs):
            raise ValueError("MCP 服务器名称已存在，请在配置中编辑")
        configs.append(cfg)
        save_settings(self.settings_path, configs)
        await self._apply(configs)
        self.config_error = ""

    async def reconnect(self, name: str) -> None:
        cfg = self._configs.get(name)
        if not cfg:
            raise ValueError("MCP 服务器不存在")
        if not cfg.enabled:
            raise ValueError("请先启用 MCP 服务器")
        await self._stop(name)
        await self._start(cfg)

    async def _apply(self, configs: list[McpServerConfig]) -> None:
        names = {cfg.name for cfg in configs}
        if len(names) != len(configs):
            raise ValueError("MCP 服务器名称不可重复")
        for name, current in list(self._configs.items()):
            new = next((cfg for cfg in configs if cfg.name == name), None)
            if new != current:
                await self._stop(name)
                self._errors.pop(name, None)
        previous = self._configs
        self._configs = {cfg.name: cfg for cfg in configs}
        to_start = []
        for cfg in configs:
            client = self._clients.get(cfg.name)
            if cfg.enabled and (client is None or not client.is_connected
                                or cfg != previous.get(cfg.name)):
                if client:
                    await self._stop(cfg.name)
                to_start.append(cfg)
        await asyncio.gather(*(self._start(cfg) for cfg in to_start))

    async def _start(self, cfg: McpServerConfig) -> None:
        client = McpClient()
        self._errors.pop(cfg.name, None)
        try:
            async with asyncio.timeout(cfg.startup_timeout):
                await self._connect(cfg, client)
                definitions = await client.list_tools()
            self._tools.extend(McpTool(client, cfg.name, definition) for definition in definitions)
            self._clients[cfg.name] = client
            log.info("mcp: server '%s' connected, %d tool(s)", cfg.name, len(definitions))
        except BaseException as exc:
            await client.close()
            if not isinstance(exc, Exception):
                raise
            message = "连接超时" if isinstance(exc, TimeoutError) else str(exc)
            for value in cfg.env.values():
                if value:
                    message = message.replace(value, "[redacted]")
            self._errors[cfg.name] = message or type(exc).__name__
            log.warning("mcp: server '%s' failed to connect", cfg.name)

    async def _stop(self, name: str) -> None:
        self._tools = [tool for tool in self._tools if tool.server_name != name]
        client = self._clients.pop(name, None)
        if client:
            try:
                await client.close()
            except Exception:
                log.warning("mcp: error closing server '%s'", name)

    def snapshot(self) -> dict[str, Any]:
        servers = []
        for cfg in self._configs.values():
            client = self._clients.get(cfg.name)
            connected = client is not None and client.is_connected
            status = "disabled" if not cfg.enabled else "connected" if connected else "error"
            tools = [tool for tool in self._tools
                     if tool.server_name == cfg.name] if connected else []
            servers.append({
                "name": cfg.name, "transport": cfg.transport, "enabled": cfg.enabled,
                "status": status, "command": cfg.command, "url": cfg.url,
                "host": cfg.host, "port": cfg.port,
                "error": self._errors.get(cfg.name, "" if status != "error" else "连接已断开"),
                "tools": [{"name": tool.name, "description": tool.description,
                           "inputSchema": tool.input_schema} for tool in tools],
            })
        return {"servers": servers, "settingsPath": str(self.settings_path),
                "configError": self.config_error}

    # 将所有已发现的 MCP 工具注册到指定 registry
    def register_tools(self, registry: ToolRegistry) -> None:
        for tool in self.get_tools():
            registry.register(tool)

    # 返回已发现的 MCP 工具列表（用于 runner 每次 run 时注入新 registry）
    def get_tools(self) -> list[McpTool]:
        return [tool for tool in self._tools if
                (client := self._clients.get(tool.server_name)) is not None and client.is_connected]

    # 关闭所有 MCP 连接并终止 stdio 子进程
    async def stop_all(self) -> None:
        for name in list(self._clients):
            await self._stop(name)

    # 根据 transport 类型建立连接
    async def _connect(self, cfg: McpServerConfig, client: McpClient) -> None:
        if cfg.transport == "stdio":
            if not cfg.command:
                raise ValueError(f"mcp server '{cfg.name}': stdio transport requires 'command'")
            await client.connect_stdio(cfg.command, cfg.args, cfg.env or None)
        elif cfg.transport == "tcp":
            await client.connect_tcp(cfg.host, cfg.port)
        elif cfg.transport == "http":
            await client.connect_http(cfg.url, cfg.bearer_token_env)
        else:
            raise ValueError(f"mcp server '{cfg.name}': unknown transport '{cfg.transport}'")
