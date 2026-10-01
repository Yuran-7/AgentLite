from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from agent_lite.core.agents.loader import AgentRegistry
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.types import LlmResponse, UsageStats
from agent_lite.core.subagent.registry import SubagentTaskManager
from agent_lite.core.subagent.tool import SpawnAgentTool


def _provider(text: str = "child done") -> Any:
    provider = AsyncMock()
    provider.chat = AsyncMock(
        return_value=LlmResponse(
            stop_reason="end_turn", tool_calls=[], text=text,
            usage=UsageStats(
                input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                cache_creation_input_tokens=0, context_pct=0.01,
            ),
        )
    )
    return provider


def _tool(
    tmp_path: Path,
    provider: Any | None = None,
    *,
    depth: int = 0,
    provider_factory: Any | None = None,
) -> tuple[SpawnAgentTool, SubagentTaskManager]:
    manager = SubagentTaskManager(lambda sid: tmp_path / sid / "tasks")
    tool = SpawnAgentTool(
        provider=provider or _provider(), parent_bus=EventBus(),
        parent_run_id="parent", permission_manager=None, max_steps=5,
        task_manager=manager, session_id="session", workspace_root=tmp_path,
        subagent_allowed_tools=[
            "read_file", "list_dir", "write_file", "shell", "spawn_agent"
        ],
        depth=depth, agent_registry=AgentRegistry(tmp_path),
        provider_factory=provider_factory,
    )
    return tool, manager


async def test_foreground_defaults_to_general_purpose(tmp_path: Path) -> None:
    tool, _manager = _tool(tmp_path, _provider("analysis complete"))
    result = await tool.invoke({"description": "analyze code", "prompt": "Inspect src"})
    assert not result.is_error
    assert result.content == "analysis complete"


async def test_background_returns_output_file_and_notification(tmp_path: Path) -> None:
    tool, manager = _tool(tmp_path)
    received: list[str] = []

    async def handler(_sid: str, _task_id: str, message: str) -> None:
        received.append(message)

    manager.set_notification_handler(handler)
    result = await tool.invoke({
        "description": "background work", "prompt": "Do work",
        "run_in_background": True,
    })
    assert "status: async_launched" in result.content
    assert "output_file:" in result.content
    task_id = result.content.split("task_id: ", 1)[1].splitlines()[0]
    task = manager._tasks[task_id]  # noqa: SLF001
    await task
    assert received and "<status>completed</status>" in received[0]
    output_file = manager.get(task_id).output_file  # type: ignore[union-attr]
    assert Path(output_file).read_text(encoding="utf-8").endswith("child done\n")


async def test_unknown_type_lists_available_agents(tmp_path: Path) -> None:
    tool, _manager = _tool(tmp_path)
    result = await tool.invoke({
        "description": "bad type", "prompt": "work", "subagent_type": "missing"
    })
    assert result.is_error
    assert "general-purpose" in result.content


async def test_nesting_limit(tmp_path: Path) -> None:
    tool, _manager = _tool(tmp_path, depth=2)
    result = await tool.invoke({"description": "too deep", "prompt": "work"})
    assert result.is_error
    assert "nesting limit" in result.content


def test_dynamic_schema_and_read_only_agents(tmp_path: Path) -> None:
    tool, _manager = _tool(tmp_path)
    enum = tool.input_schema["properties"]["subagent_type"]["enum"]  # type: ignore[index]
    assert set(enum) >= {"general-purpose", "explore", "plan"}
    explore = tool._agent_registry.get("explore")  # noqa: SLF001
    assert explore is not None
    effective = tool._agent_registry.effective_tools(  # noqa: SLF001
        explore, tool._subagent_allowed_tools  # noqa: SLF001
    )
    assert "write_file" not in effective
    assert "shell" not in effective
    assert "spawn_agent" not in effective


async def test_exact_model_uses_provider_factory(tmp_path: Path) -> None:
    directory = tmp_path / ".agentlite" / "agents"
    directory.mkdir(parents=True)
    (directory / "special.md").write_text(
        """---
name: special
description: Special model
model: vendor-model-id
---
Do the special task.
""",
        encoding="utf-8",
    )
    child_provider = _provider("special done")
    calls: list[str] = []

    def factory(model: str) -> Any:
        calls.append(model)
        return child_provider

    tool, _manager = _tool(tmp_path, provider_factory=factory)
    result = await tool.invoke({
        "description": "special work", "prompt": "work", "subagent_type": "special"
    })
    assert result.content == "special done"
    assert calls == ["vendor-model-id"]


async def test_background_true_in_definition_is_forced(tmp_path: Path) -> None:
    directory = tmp_path / ".agentlite" / "agents"
    directory.mkdir(parents=True)
    (directory / "always-bg.md").write_text(
        """---
name: always-bg
description: Always background
background: true
---
Work in the background.
""",
        encoding="utf-8",
    )
    tool, manager = _tool(tmp_path)
    result = await tool.invoke({
        "description": "background", "prompt": "work", "subagent_type": "always-bg"
    })
    assert "async_launched" in result.content
    await manager.cancel_all()
