"""Shared MCP configuration; validate the entire file before replacing live connections."""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_lite.core.config import McpServerConfig


def settings_path() -> Path:
    return Path(os.environ.get("AGENTLITE_MCP_SETTINGS", "~/.agentlite/mcp.json")).expanduser()


def parse_server(entry: Any) -> McpServerConfig:
    fields = set(McpServerConfig.__dataclass_fields__)
    if not isinstance(entry, dict) or set(entry) - fields:
        raise ValueError("MCP 服务器字段无效")
    name = entry.get("name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("MCP 名称请使用 1–64 位字母、数字、下划线或连字符")
    strings = ("transport", "command", "host", "url", "bearer_token_env")
    if any(key in entry and not isinstance(entry[key], str) for key in strings):
        raise ValueError("MCP command、transport、host、url 和 bearer_token_env 必须是字符串")
    if "enabled" in entry and type(entry["enabled"]) is not bool:
        raise ValueError("MCP enabled 必须是布尔值")
    args, env = entry.get("args", []), entry.get("env", {})
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("MCP args 必须是字符串数组")
    if not isinstance(env, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
    ):
        raise ValueError("MCP env 必须是字符串键值表")
    port, timeout = entry.get("port", 3000), entry.get("startup_timeout", 10.0)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("MCP port 必须是 1–65535 的整数")
    if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
        raise ValueError("MCP startup_timeout 必须在 1–120 秒之间")
    cfg = McpServerConfig(**entry)
    if cfg.transport not in ("stdio", "tcp", "http"):
        raise ValueError("MCP transport 必须是 stdio、http 或 tcp")
    if cfg.transport == "stdio" and not cfg.command.strip():
        raise ValueError("STDIO 服务器必须填写 command")
    if cfg.transport == "http":
        url = urlsplit(cfg.url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError("HTTP 服务器必须填写有效的 http(s) URL，凭据请使用环境变量")
    if cfg.bearer_token_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cfg.bearer_token_env):
        raise ValueError("bearer_token_env 必须是环境变量名")
    return cfg


def load_settings(path: Path, fallback: list[McpServerConfig]) -> list[McpServerConfig]:
    if not path.exists():
        return list(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 MCP 配置：{path}") from exc
    if not isinstance(data, dict) or set(data) - {"servers", "$schema"}:
        raise ValueError("MCP 配置只支持 servers 数组")
    if not isinstance(data.get("servers"), list):
        raise ValueError("MCP servers 必须是数组")
    configs = [parse_server(entry) for entry in data["servers"]]
    if len({cfg.name for cfg in configs}) != len(configs):
        raise ValueError("MCP 服务器名称不可重复")
    return configs


def save_settings(path: Path, configs: list[McpServerConfig]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"servers": [asdict(cfg) for cfg in configs]}, ensure_ascii=False, indent=2)
    descriptor, temp = tempfile.mkstemp(prefix="mcp-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data + "\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
