"""Streamable HTTP transport for MCP tool discovery and calls (JSON and SSE responses)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values


class McpHttpTransport:
    def __init__(self, url: str, bearer_token_env: str = "") -> None:
        headers = {"Accept": "application/json, text/event-stream"}
        if bearer_token_env:
            token = os.environ.get(bearer_token_env) or dotenv_values(
                Path.home() / ".agentlite/.env"
            ).get(bearer_token_env)
            if not token:
                raise ValueError(f"未设置 MCP Token 环境变量：{bearer_token_env}")
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(headers=headers, timeout=60.0)
        self._url = url
        self._session_id = ""
        self._protocol = ""
        self.connected = False

    async def post(self, message: dict[str, Any]) -> dict[str, Any]:
        headers = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol:
            headers["MCP-Protocol-Version"] = self._protocol
        try:
            async with self._client.stream(
                "POST", self._url, json=message, headers=headers
            ) as resp:
                if resp.status_code in (401, 403):
                    self.connected = False
                    raise ValueError(
                        "服务器需要身份验证。请配置 bearer_token_env；暂不支持 OAuth 登录"
                    )
                if resp.status_code == 404:
                    self.connected = False
                    raise ValueError("MCP 地址不存在或会话已过期，请检查 URL 后重连")
                if resp.is_error:
                    raise ValueError(f"MCP HTTP 请求失败：{resp.status_code}")
                if resp.headers.get("Mcp-Session-Id"):
                    self._session_id = resp.headers["Mcp-Session-Id"]
                if "id" not in message:
                    return {}
                if "text/event-stream" in resp.headers.get("content-type", ""):
                    data: list[str] = []
                    size = 0
                    async for line in resp.aiter_lines():
                        size += len(line.encode("utf-8"))
                        if size > 64 * 1024 * 1024:
                            raise ValueError("MCP HTTP 响应超过 64 MB")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip(" "))
                        elif not line and data:
                            result = self._matching(json.loads("\n".join(data)), message["id"])
                            data.clear()
                            if result is not None:
                                return result
                    if data:
                        result = self._matching(json.loads("\n".join(data)), message["id"])
                        if result is not None:
                            return result
                else:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > 64 * 1024 * 1024:
                            raise ValueError("MCP HTTP 响应超过 64 MB")
                        chunks.append(chunk)
                    result = self._matching(json.loads(b"".join(chunks)), message["id"])
                    if result is not None:
                        return result
        except httpx.HTTPError as exc:
            self.connected = False
            raise ValueError("MCP HTTP 连接失败或请求超时") from exc
        raise ValueError("MCP HTTP 未返回对应的 JSON-RPC 响应")

    def _matching(self, data: Any, request_id: Any) -> dict[str, Any] | None:
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or str(item.get("id")) != str(request_id):
                continue
            if "error" in item:
                # 不向前端透传可能包含 token 或 header 的服务端错误正文。
                raise ValueError("MCP 服务器返回 JSON-RPC 错误")
            result = item.get("result")
            if not isinstance(result, dict):
                raise ValueError("MCP result 必须是对象")
            return result
        return None

    def initialized(self, protocol: str) -> None:
        self._protocol = protocol
        self.connected = True

    async def close(self) -> None:
        try:
            if self._session_id:
                await self._client.delete(
                    self._url, headers={"Mcp-Session-Id": self._session_id}, timeout=5.0
                )
        except httpx.HTTPError:
            pass
        finally:
            self.connected = False
            await self._client.aclose()
