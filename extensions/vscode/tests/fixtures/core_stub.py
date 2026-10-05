from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import agent_lite.core.app as app_module
import agent_lite.core.runner as runner_module
from agent_lite.core.bus.events import LlmTokenEvent
from agent_lite.core.config import AgentLiteConfig
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from agent_lite.core.permissions.manager import PermissionManager


class ScriptedProvider:
    # 用确定性的模型替身运行真实 AgentRunner 与工具权限链路。
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        **kwargs: Any,
    ) -> LlmResponse:
        step = int(kwargs.get("step", 0))
        goals = [
            str(message["content"])
            for message in messages
            if message.get("role") == "user" and isinstance(message.get("content"), str)
        ]
        goal = goals[-1] if goals else "hello"
        ts = datetime.now(UTC).isoformat()
        if goal.startswith("cancel"):
            await bus.publish(LlmTokenEvent(run_id=run_id, token="等待取消", ts=ts))
            await asyncio.Event().wait()
        if step == 1 and goal == "mcp echo":
            return LlmResponse(
                stop_reason="tool_use",
                tool_calls=[ToolCallBlock(
                    uuid.uuid4().hex, "echo__echo", {"text": "MCP roundtrip 8127"}
                )],
                usage=UsageStats(12, 3),
            )
        if step == 1 and ("read" in goal or "permission" in goal):
            tool = "write_file" if "permission" in goal else "read_file"
            params: dict[str, object] = (
                {"path": "result.txt", "content": "批准后的内容"}
                if tool == "write_file"
                else {"path": "sample.txt"}
            )
            return LlmResponse(
                stop_reason="tool_use",
                tool_calls=[ToolCallBlock(uuid.uuid4().hex, tool, params)],
                usage=UsageStats(12, 3),
            )
        await bus.publish(LlmTokenEvent(run_id=run_id, token="你好，完成了", ts=ts))
        return LlmResponse(stop_reason="end_turn", text="你好，完成了", usage=UsageStats(12, 5))


# 返回模型替身，不创建外部 API 连接。
def provider_factory(*args: Any, **kwargs: Any) -> ScriptedProvider:
    return ScriptedProvider()


# 使用临时配置隔离会话、记忆、日志和权限，禁止读取用户全局策略。
def config_factory() -> AgentLiteConfig:
    config = AgentLiteConfig()
    config.port = int(os.environ["AGENTLITE_PORT"])
    root = Path(os.environ["AGENTLITE_TEST_DIR"])
    os.environ["AGENTLITE_MCP_SETTINGS"] = str(root / "mcp.json")
    config.logging.file = str(root / "core.log")
    config.session.dir = str(root / "sessions")
    config.memory.dir = str(root / "memory.db")
    config.trace.enabled = False
    config.web.enabled = False
    config.permission.timeout_s = 0.4
    # 此夹具验证原有四种人工审批决策，不依赖新会话的 Auto 默认值。
    config.permission.default_mode = "manual"
    return config


# 为真实 core 注入模型与权限存储替身，其他协议和业务行为保持原实现。
def main() -> None:
    app_module.get_config = config_factory
    app_module.create_llm_provider = provider_factory
    runner_module.create_llm_provider = provider_factory
    app_module.PermissionManager = lambda **kwargs: PermissionManager(timeout_s=0.4)
    app_module.run()


if __name__ == "__main__":
    main()
