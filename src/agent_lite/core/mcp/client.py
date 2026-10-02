from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_lite.core.mcp.http import McpHttpTransport

log = logging.getLogger(__name__)


class McpServerUnavailableError(Exception):
    pass


class McpToolError(Exception):
    """MCP server 返回的应用层错误（连接正常，但工具调用失败）"""
    pass


@dataclass
class McpToolDef:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


# 通过 stdio 或 TCP 与 MCP server 通信的 JSON-RPC 2.0 客户端
class McpClient:
    def __init__(self) -> None:
        self._id = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._transport = ""
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._http: McpHttpTransport | None = None

    @property
    def is_connected(self) -> bool:
        if self._http is not None:
            return self._http.connected
        if self._transport == "stdio":
            return self._proc is not None and self._proc.returncode is None
        if self._transport == "tcp":
            writer = getattr(self, "_tcp_writer", None)
            return (self._reader is not None and not self._reader.at_eof()
                    and writer is not None and not writer.is_closing())
        return False

    async def connect_http(self, url: str, bearer_token_env: str = "") -> None:
        self._http = McpHttpTransport(url, bearer_token_env)
        self._transport = "http"
        await self._initialize()

    _STREAM_LIMIT = 64 * 1024 * 1024  # 64 MB，防止大响应触发 LimitOverrunError

    # 启动 stdio 子进程并完成 MCP initialize 握手
    async def connect_stdio(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> None:
        import os
        merged_env = {**os.environ, **(env or {})}
        # Windows npm 的 .cmd 包装器不能直接交给 CreateProcess；使用同目录的 Node CLI。
        executable = shutil.which(command, path=merged_env.get("PATH")) or command
        if sys.platform == "win32" and Path(executable).suffix.lower() in (".cmd", ".bat"):
            wrapper = Path(executable)
            stem = wrapper.stem.lower()
            cli = wrapper.parent / "node_modules/npm/bin" / f"{stem}-cli.js"
            node_args: list[str] = []
            if stem not in ("npx", "npm") or not cli.is_file():
                # npm installs one .cmd shim per package bin, including scoped packages.
                # Extract only the literal Node invocation; never interpret batch syntax.
                try:
                    shim = wrapper.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    shim = ""
                invocation = re.search(
                    r'"%_prog%"((?: --[A-Za-z0-9_=.-]+)*) '
                    r'"%dp0%[\\/]node_modules[\\/]([^"%\r\n]+)" %\*', shim,
                )
                if invocation:
                    candidate = wrapper.parent / "node_modules" / invocation[2].replace("\\", "/")
                    if (candidate.suffix.lower() in (".js", ".cjs", ".mjs")
                            and candidate.resolve().is_relative_to(
                                (wrapper.parent / "node_modules").resolve()
                            ) and candidate.is_file()):
                        cli = candidate
                        node_args = invocation[1].split()
                    else:
                        invocation = None
                if not invocation:
                    raise ValueError(
                        "Windows MCP 请使用可执行文件、Node/Python 脚本或标准 npm 启动器"
                    )
            node = wrapper.parent / "node.exe"
            executable = str(node) if node.is_file() else (
                shutil.which("node", path=merged_env.get("PATH")) or "node"
            )
            args = [*node_args, str(cli), *args]
        self._proc = await asyncio.create_subprocess_exec(
            executable, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            limit=self._STREAM_LIMIT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        self._reader = self._proc.stdout
        self._writer_proc = self._proc.stdin
        self._transport = "stdio"
        # 后台持续读取 stderr，防止管道缓冲区满导致子进程阻塞
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._initialize()

    # 通过 TCP 连接到 MCP server 并完成 initialize 握手
    async def connect_tcp(self, host: str, port: int) -> None:
        self._reader, tcp_writer = await asyncio.open_connection(
            host, port, limit=self._STREAM_LIMIT
        )
        self._tcp_writer = tcp_writer
        self._transport = "tcp"
        await self._initialize()

    # 发送 initialize 请求完成 MCP 握手
    async def _initialize(self) -> None:
        result = await self._call("initialize", {
            "protocolVersion": "2025-03-26" if self._http else "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "agentlite", "version": "0.1"},
        })
        if self._http:
            self._http.initialized(str(result.get("protocolVersion", "2025-03-26")))
        await self._notify("notifications/initialized", {})

    # 列出 MCP server 提供的工具定义
    async def list_tools(self) -> list[McpToolDef]:
        tools: list[McpToolDef] = []
        params: dict[str, Any] = {}
        seen: set[str] = set()
        while True:
            response = await self._call("tools/list", params)
            for t in response.get("tools", []):
                tools.append(McpToolDef(
                    name=t.get("name", ""), description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {}),
                ))
            cursor = response.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or cursor in seen:
                raise McpToolError("MCP tools/list returned an invalid cursor")
            seen.add(cursor)
            params = {"cursor": cursor}

    # 调用工具并拼接文本，分别报告连接异常与工具错误
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        response = await self._call("tools/call", {"name": name, "arguments": arguments})
        if response.get("isError"):
            raise McpToolError("MCP 工具执行失败")
        parts: list[str] = []
        for item in response.get("content", []):
            if item.get("type") == "text":
                parts.append(str(item["text"]))
        return "\n".join(parts)

    # 后台任务：持续读取 stderr 并记录日志，防止管道缓冲区满
    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                stderr_line = line.decode(errors="replace").rstrip()
                if stderr_line:
                    log.debug("mcp stderr: %s", stderr_line)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.debug("mcp stderr drain stopped", exc_info=True)

    # 关闭连接并终止 stdio 子进程
    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None
        # 先取消 stderr 读取任务
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None
        if self._transport == "stdio" and self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                try:
                    self._proc.kill()
                    await self._proc.wait()
                except Exception:
                    pass
        elif self._transport == "tcp":
            writer = getattr(self, "_tcp_writer", None)
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    # 发送 JSON-RPC 请求并等待响应；id 比较用字符串兼容服务端返回字符串 id 的情况
    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        req_id = self._id
        req_id_str = str(req_id)
        request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        async with self._lock:
            if self._http is not None:
                try:
                    return await self._http.post(request)
                except ValueError as exc:
                    raise McpServerUnavailableError(str(exc)) from exc
            await self._write_line(json.dumps(request))
            while True:
                line = await self._read_line()
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("mcp: ignoring non-JSON line: %r", line[:200])
                    continue
                msg_id = msg.get("id")
                if msg_id is None:
                    # server-initiated notification，忽略
                    log.debug("mcp: received server notification: %s", msg.get("method"))
                    continue
                if str(msg_id) == req_id_str:
                    if "error" in msg:
                        err = msg["error"]
                        raise McpToolError(
                            f"{err.get('message', str(err))} (code={err.get('code')})"
                        )
                    result: dict[str, Any] = msg.get("result", {})
                    return result

    # 发送 JSON-RPC 通知（无响应）
    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        notification = {"jsonrpc": "2.0", "method": method, "params": params}
        if self._http is not None:
            await self._http.post(notification)
            return
        await self._write_line(json.dumps(notification))

    # 向 MCP server 写入一行 JSON
    async def _write_line(self, line: str) -> None:
        data = (line + "\n").encode()
        if self._transport == "stdio":
            w = self._proc.stdin if self._proc else None
            if w is None:
                raise McpServerUnavailableError("stdio writer unavailable")
            w.write(data)
            await w.drain()
        elif self._transport == "tcp":
            w = getattr(self, "_tcp_writer", None)
            if w is None:
                raise McpServerUnavailableError("tcp writer unavailable")
            w.write(data)
            await w.drain()

    # 从 MCP server 读取一行 JSON；跳过空行，仅 EOF（b""）才视为连接断开
    async def _read_line(self) -> str:
        if self._reader is None:
            raise McpServerUnavailableError("reader unavailable")
        while True:
            try:
                data = await asyncio.wait_for(self._reader.readline(), timeout=30.0)
            except TimeoutError:
                raise McpServerUnavailableError("MCP server read timeout")
            except asyncio.LimitOverrunError as exc:
                raise McpServerUnavailableError(
                    f"MCP response too large (>{self._STREAM_LIMIT // 1024 // 1024}MB): {exc}"
                ) from exc
            if data == b"":
                raise McpServerUnavailableError("MCP server closed connection")
            line = data.decode(errors="replace").strip()
            if line:
                return line
