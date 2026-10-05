"""Hot-loaded model profiles. Secrets stay in environment variables / .env files."""
from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from agent_lite.core.config import LlmConfig
from agent_lite.core.llm.reasoning import EFFORTS, supported_efforts


def model_settings(workspace: str | None = None) -> dict[str, Any]:
    paths = [Path.home() / ".agentlite/settings.json"]
    models: dict[str, dict[str, Any]] = {}
    default = ""
    for path in dict.fromkeys(paths):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法读取模型配置：{path}") from exc
        if not isinstance(data, dict) or set(data) - {"models", "defaultModel", "$schema"}:
            raise ValueError(f"模型配置只支持 models 和 defaultModel：{path}")
        entries = data.get("models", [])
        if not isinstance(entries, list):
            raise ValueError(f"models 必须是数组：{path}")
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {
                "id", "name", "model", "protocol", "baseUrl", "apiKeyEnv", "contextWindow",
                "apiMode", "reasoningEffort"
            }:
                raise ValueError("模型字段无效；密钥请放入 ~/.agentlite/.env，通过 apiKeyEnv 引用")
            if "contextWindow" in entry and (
                type(entry["contextWindow"]) is not int or entry["contextWindow"] <= 0
            ):
                raise ValueError("contextWindow 必须是正整数")
            if any(not isinstance(v, str) for k, v in entry.items() if k != "contextWindow"):
                raise ValueError("模型配置字段必须是字符串")
            if any(not entry.get(k, "").strip() for k in ("id", "model", "protocol")):
                raise ValueError("每个模型必须填写 id、model、protocol")
            if entry["id"] in seen or entry["protocol"] not in {"openai", "anthropic"}:
                raise ValueError("模型 id 不可重复，protocol 必须是 openai 或 anthropic")
            if entry.get("apiKeyEnv") and not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", entry["apiKeyEnv"]
            ):
                raise ValueError(
                    "apiKeyEnv 应填写环境变量名（例如 MY_MODEL_API_KEY），"
                    "密钥请写入 ~/.agentlite/.env，而不是这个字段"
                )
            seen.add(entry["id"])
            if "apiMode" in entry and (
                entry["protocol"] != "openai"
                or entry["apiMode"] not in {"chat_completions", "responses"}
            ):
                raise ValueError("apiMode 仅用于 openai，必须是 chat_completions 或 responses")
            models[entry["id"]] = dict(entry)
            if "reasoningEffort" in entry and (
                entry["reasoningEffort"] not in EFFORTS
                or entry["reasoningEffort"] not in supported_efforts(
                    entry["model"], entry["protocol"]
                )
            ):
                raise ValueError("reasoningEffort 必须是当前模型支持的推理强度")
        if "defaultModel" in data:
            if not isinstance(data["defaultModel"], str):
                raise ValueError("defaultModel 必须是模型 id 字符串")
            default = data["defaultModel"]
        elif entries:
            default = entries[0]["id"]
    if default and default not in models:
        raise ValueError("defaultModel 引用的模型 id 不存在")
    return {"models": list(models.values()), "defaultModel": default,
            "settingsPath": str(paths[-1])}


def resolve_model(config: LlmConfig, model_id: str | None, workspace: str | None) -> LlmConfig:
    settings = model_settings(workspace)
    selected = settings["defaultModel"] if model_id is None else model_id
    if not selected:
        return config
    profile = next((p for p in settings["models"] if p["id"] == selected), None)
    if profile is None:
        raise ValueError(f"模型配置已移除：{selected}，请重新选择模型")
    env_name = profile.get("apiKeyEnv") or (
        "OPENAI_API_KEY" if profile["protocol"] == "openai" else "ANTHROPIC_API_KEY"
    )
    env_path = Path.home() / ".agentlite/.env"
    values = dotenv_values(env_path, interpolate=False) if env_path.is_file() else {}
    key = os.environ.get(env_name) or values.get(env_name)
    if not key:
        raise ValueError(f"模型密钥未配置：请在 ~/.agentlite/.env 或系统环境变量中设置 {env_name}")
    return replace(config, protocol=profile["protocol"], default_model=profile["model"],
                   api_mode=profile.get("apiMode", "chat_completions"),
                   reasoning_effort=profile.get("reasoningEffort", ""),
                   base_url=profile.get("baseUrl", ""), api_key=key,
                   context_window=profile.get("contextWindow", config.context_window))
