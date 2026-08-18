from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_lite.core.agents.loader import AgentProfile
from agent_lite.core.config import WebConfig
from agent_lite.core.events.bus import EventBus
from agent_lite.core.events.writer import EventAppender
from agent_lite.core.llm.types import LlmResponse, UsageStats
from agent_lite.core.subagent.registry import BackgroundTaskRegistry
from agent_lite.core.subagent.tool import AgentResultTool, SpawnAgentTool


def _make_provider(result_text: str = "child done") -> Any:
    provider = AsyncMock()
    provider.chat = AsyncMock(
        return_value=LlmResponse(
            stop_reason="end_turn",
            tool_calls=[],
            text=result_text,
            usage=UsageStats(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                context_pct=0.01,
            ),
        )
    )
    return provider


def _make_tool(
    tmp_path: Path,
    provider: Any = None,
    depth: int = 0,
) -> tuple[SpawnAgentTool, BackgroundTaskRegistry, EventBus]:
    bus = EventBus()
    registry = BackgroundTaskRegistry()
    tool = SpawnAgentTool(
        provider=provider or _make_provider(),
        parent_bus=bus,
        parent_run_id="parent-run-01",
        permission_manager=None,
        max_steps=5,
        task_registry=registry,
        session_id="sess-test",
        depth=depth,
    )
    return tool, registry, bus


# 功能：前台模式下 spawn_agent 应阻塞直到子 agent 完成并返回其结果
# 设计：使用返回 end_turn 的 mock provider，验证 tool_result.content 包含 provider 返回的文字
@pytest.mark.asyncio
async def test_foreground_returns_result(tmp_path: Path) -> None:
    tool, _, _ = _make_tool(tmp_path, _make_provider("analysis complete"))
    result = await tool.invoke({
        "description": "分析代码",
        "prompt": "分析 src/ 目录",
    })
    assert not result.is_error
    assert "analysis complete" in result.content
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / ".tasks").exists()


# 功能：后台模式应立即返回含 run_id 的消息，不阻塞等待子 agent
# 设计：run_in_background=true 后验证返回消息含 "run_id=" 并且任务注册表已有对应条目
@pytest.mark.asyncio
async def test_background_returns_run_id(tmp_path: Path) -> None:
    tool, registry, _ = _make_tool(tmp_path)
    result = await tool.invoke({
        "description": "后台任务",
        "prompt": "做点事",
        "run_in_background": True,
    })
    assert not result.is_error
    assert "run_id=" in result.content
    # extract run_id from message
    run_id = result.content.split("run_id=")[1].split(".")[0]
    assert registry.get(run_id) is not None


# 功能：后台任务未完成时 agent_result 应返回 "still running"
# 设计：用 Event 阻塞 provider.chat，在未等待任务完成时查询 agent_result
@pytest.mark.asyncio
async def test_agent_result_pending(tmp_path: Path) -> None:
    event = asyncio.Event()

    async def slow_chat(*args: Any, **kwargs: Any) -> LlmResponse:
        await event.wait()
        return LlmResponse(
            stop_reason="end_turn",
            tool_calls=[],
            text="done",
            usage=UsageStats(0, 0, 0, 0, 0.0),
        )

    provider = MagicMock()
    provider.chat = slow_chat

    tool, registry, _ = _make_tool(tmp_path, provider)
    spawn_result = await tool.invoke({
        "description": "slow task",
        "prompt": "do something slow",
        "run_in_background": True,
    })
    run_id = spawn_result.content.split("run_id=")[1].split(".")[0]

    result_tool = AgentResultTool(registry)
    result = await result_tool.invoke({"run_id": run_id})
    assert result.content == "still running"
    assert not result.is_error

    event.set()
    await asyncio.sleep(0.05)


# 功能：agent_result 等待期间子 agent 完成时应立即返回最终结果
# 设计：用事件阻塞 provider，再通过短延迟释放事件，验证无需等满超时时间即可取得结果
@pytest.mark.asyncio
async def test_agent_result_wait_returns_when_completed(tmp_path: Path) -> None:
    event = asyncio.Event()

    async def delayed_chat(*args: Any, **kwargs: Any) -> LlmResponse:
        await event.wait()
        return LlmResponse(
            stop_reason="end_turn",
            tool_calls=[],
            text="completed while waiting",
            usage=UsageStats(0, 0, 0, 0, 0.0),
        )

    provider = MagicMock()
    provider.chat = delayed_chat
    tool, registry, _ = _make_tool(tmp_path, provider)
    spawn_result = await tool.invoke({
        "description": "delayed task",
        "prompt": "finish shortly",
        "run_in_background": True,
    })
    run_id = spawn_result.content.split("run_id=")[1].split(".")[0]
    asyncio.get_running_loop().call_later(0.01, event.set)

    result = await AgentResultTool(registry).invoke({
        "run_id": run_id,
        "timeout_seconds": 1,
    })

    assert not result.is_error
    assert result.content == "completed while waiting"


# 功能：agent_result 等待超时只应停止等待，不应取消仍在运行的子 agent
# 设计：保持 provider 阻塞超过短超时，断言返回 still running 且后台 task 仍存活
@pytest.mark.asyncio
async def test_agent_result_timeout_does_not_cancel_task(tmp_path: Path) -> None:
    event = asyncio.Event()

    async def slow_chat(*args: Any, **kwargs: Any) -> LlmResponse:
        await event.wait()
        return LlmResponse(
            stop_reason="end_turn",
            tool_calls=[],
            text="done later",
            usage=UsageStats(0, 0, 0, 0, 0.0),
        )

    provider = MagicMock()
    provider.chat = slow_chat
    tool, registry, _ = _make_tool(tmp_path, provider)
    spawn_result = await tool.invoke({
        "description": "slow task",
        "prompt": "keep working",
        "run_in_background": True,
    })
    run_id = spawn_result.content.split("run_id=")[1].split(".")[0]

    result = await AgentResultTool(registry).invoke({
        "run_id": run_id,
        "timeout_seconds": 0.01,
    })
    entry = registry.get(run_id)
    assert entry is not None
    task, _ = entry

    assert result.content == "still running"
    assert not result.is_error
    assert not task.done()

    event.set()
    await asyncio.wait_for(task, timeout=1.0)


# 功能：后台任务完成后 agent_result 应返回子 agent 的最终文本
# 设计：等待后台任务完成后查询结果，断言返回内容与 provider 结果一致
@pytest.mark.asyncio
async def test_agent_result_done(tmp_path: Path) -> None:
    tool, registry, _ = _make_tool(tmp_path, _make_provider("final answer"))
    spawn_result = await tool.invoke({
        "description": "bg task",
        "prompt": "do it",
        "run_in_background": True,
    })
    run_id = spawn_result.content.split("run_id=")[1].split(".")[0]

    entry = registry.get(run_id)
    assert entry is not None
    task, _ = entry
    await asyncio.wait_for(task, timeout=5.0)

    result_tool = AgentResultTool(registry)
    result = await result_tool.invoke({"run_id": run_id})
    assert not result.is_error
    assert "final answer" in result.content


# 功能：depth=2 时调用 spawn_agent 应返回 is_error=True（嵌套限制）
# 设计：构造 depth=2 的工具，断言 invoke 直接返回错误而不调用 provider
@pytest.mark.asyncio
async def test_nesting_limit(tmp_path: Path) -> None:
    provider = _make_provider()
    tool, _, _ = _make_tool(tmp_path, provider, depth=2)
    result = await tool.invoke({
        "description": "nested",
        "prompt": "do nested work",
    })
    assert result.is_error
    assert "nesting limit" in result.content
    provider.chat.assert_not_called()


# 功能：agent_result 查询不存在的 run_id 应返回 is_error=True
# 设计：空 registry 中查询随机 run_id，验证错误消息含 "Unknown"
@pytest.mark.asyncio
async def test_agent_result_unknown_run_id(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry()
    tool = AgentResultTool(registry)
    result = await tool.invoke({"run_id": "nonexistent-id"})
    assert result.is_error
    assert "Unknown" in result.content


# 功能：SubagentStartedEvent 应在前台 spawn 时发布到父 bus
# 设计：订阅父 bus 收集所有事件，断言 subagent.started 出现，且 parent_run_id 和 description 正确
@pytest.mark.asyncio
async def test_foreground_publishes_started_event(tmp_path: Path) -> None:
    from agent_lite.core.bus.events import SubagentStartedEvent

    tool, _, bus = _make_tool(tmp_path)
    events: list[Any] = []

    async def _collect(e: Any) -> None:
        events.append(e)

    bus.subscribe(_collect)

    await tool.invoke({
        "description": "test task",
        "prompt": "test prompt",
    })
    started = [e for e in events if isinstance(e, SubagentStartedEvent)]
    assert len(started) == 1
    assert started[0].parent_run_id == "parent-run-01"
    assert started[0].description == "test task"


# 功能：session 统一事件日志不应因 subagent 向父总线桥接而重复记录
# 设计：关闭子级独立日志并让父总线统一追加，断言子 agent 每类生命周期事件仅出现一次
@pytest.mark.asyncio
async def test_shared_event_log_records_subagent_events_once(tmp_path: Path) -> None:
    bus = EventBus()
    event_file = tmp_path / "events.jsonl"
    EventAppender(event_file).subscribe(bus)
    tool = SpawnAgentTool(
        provider=_make_provider(),
        parent_bus=bus,
        parent_run_id="parent",
        permission_manager=None,
        max_steps=5,
        task_registry=BackgroundTaskRegistry(),
        session_id="session",
    )

    result = await tool.invoke({"description": "child", "prompt": "do work"})

    assert not result.is_error
    events = [
        json.loads(line)
        for line in event_file.read_text(encoding="utf-8").splitlines()
    ]
    event_types = [event["type"] for event in events]
    assert event_types.count("subagent.started") == 1
    assert event_types.count("step.started") == 1
    assert event_types.count("step.finished") == 1
    assert event_types.count("subagent.finished") == 1


# 功能：默认子 agent 能力上限不包含联网工具
# 设计：即使传入启用的 WebConfig，未显式加入全局上限时 registry 也不注册 web 工具
def test_default_child_cap_blocks_web_tools(tmp_path: Path) -> None:
    tool, _, bus = _make_tool(tmp_path)
    tool._web_config = WebConfig()

    registry = tool._build_child_registry(bus, "child-run", profile=None)

    assert registry.get("web_search") is None
    assert registry.get("web_fetch") is None


# 功能：全局能力上限可显式向匿名子 agent 开放联网工具
# 设计：同时配置 web 和 allowlist，确认两个条件缺一不可
def test_child_cap_can_explicitly_allow_web_tools(tmp_path: Path) -> None:
    bus = EventBus()
    tool = SpawnAgentTool(
        provider=_make_provider(),
        parent_bus=bus,
        parent_run_id="parent",
        permission_manager=None,
        max_steps=5,
        task_registry=BackgroundTaskRegistry(),
        session_id="session",
        web_config=WebConfig(),
        subagent_allowed_tools=["read_file", "web_search", "web_fetch"],
    )

    registry = tool._build_child_registry(bus, "child-run", profile=None)

    assert registry.get("web_search") is not None
    assert registry.get("web_fetch") is not None
    assert registry.get("bash") is None


# 功能：角色白名单只能继续收窄全局能力上限，不能因为全局允许而自动获得联网能力
# 设计：全局包含 web_search，但角色只包含 read_file，最终 registry 不应含 web_search
def test_profile_allowlist_intersects_global_child_cap(tmp_path: Path) -> None:
    bus = EventBus()
    tool = SpawnAgentTool(
        provider=_make_provider(),
        parent_bus=bus,
        parent_run_id="parent",
        permission_manager=None,
        max_steps=5,
        task_registry=BackgroundTaskRegistry(),
        session_id="session",
        web_config=WebConfig(),
        subagent_allowed_tools=["read_file", "web_search"],
    )
    profile = AgentProfile(
        name="reader",
        description="read only",
        system_prompt="read",
        allowed_tools=["read_file"],
    )

    registry = tool._build_child_registry(bus, "child-run", profile=profile)

    assert registry.get("read_file") is not None
    assert registry.get("web_search") is None


# 功能：未知角色不能静默降级为拥有匿名子 agent 权限的运行
# 设计：命名不存在的 profile，确认在创建 child registry 前直接失败
@pytest.mark.asyncio
async def test_unknown_profile_is_rejected(tmp_path: Path) -> None:
    tool, _, _ = _make_tool(tmp_path)
    result = await tool.invoke(
        {
            "description": "unknown role",
            "prompt": "do work",
            "subagent_type": "does-not-exist",
        }
    )
    assert result.is_error
    assert result.error_type == "schema_error"
