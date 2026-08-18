from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from agent_lite.core.bus.events import PlanUpdatedEvent
from agent_lite.core.config import AgentLiteConfig
from agent_lite.core.events.bus import EventBus
from agent_lite.core.runner import AgentRunner
from agent_lite.core.tools.builtin.update_plan import UpdatePlanTool


# 功能：验证 update_plan 发布完整计划事件并返回 Codex 风格成功文本
# 设计：使用内联事件收集器断言 run_id、解释和三个计划状态都被保留
async def test_update_plan_publishes_event() -> None:
    bus = EventBus()
    events: list[object] = []

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    result = await UpdatePlanTool(bus, "run-1").invoke({
        "explanation": "start",
        "plan": [
            {"step": "inspect", "status": "completed"},
            {"step": "implement", "status": "in_progress"},
            {"step": "verify", "status": "pending"},
        ],
    })

    assert result.content == "Plan updated"
    assert not result.is_error
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, PlanUpdatedEvent)
    assert event.run_id == "run-1"
    assert event.explanation == "start"
    assert [item.status for item in event.plan] == [
        "completed", "in_progress", "pending"
    ]


# 功能：验证 update_plan 拒绝多个 in_progress 步骤
# 设计：多进行中状态会让进度展示失去唯一当前步骤，工具应返回可重试的运行时错误
async def test_update_plan_rejects_multiple_in_progress() -> None:
    bus = EventBus()
    events: list[object] = []

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    result = await UpdatePlanTool(bus, "run-1").invoke({
        "plan": [
            {"step": "one", "status": "in_progress"},
            {"step": "two", "status": "in_progress"},
        ],
    })

    assert result.is_error
    assert result.error_type == "runtime_error"
    assert events == []


# 功能：验证无效状态在工具参数模型层被拒绝
# 设计：直接调用参数模型覆盖 schema 校验边界，避免错误计划进入事件总线
def test_update_plan_params_validate_status() -> None:
    from agent_lite.core.tools.builtin.update_plan import UpdatePlanParams

    try:
        UpdatePlanParams.model_validate({"plan": [{"step": "x", "status": "bad"}]})
    except ValidationError:
        return
    raise AssertionError("invalid plan status should fail validation")


# 功能：验证运行时工具注册只暴露 update_plan，不再暴露 task_* 工具
# 设计：直接构建主 agent registry，覆盖默认工具面而不依赖 LLM 或 daemon
def test_runner_registry_replaces_task_tools(tmp_path: Path) -> None:
    bus = EventBus()
    registry = AgentRunner(AgentLiteConfig(), events_file=tmp_path / "events.jsonl")._build_registry(
        bus=bus,
        run_id="run-1",
    )
    names = {schema["name"] for schema in registry.tool_schemas()}

    assert "update_plan" in names
    assert not names.intersection({"task_create", "task_update", "task_list", "task_get"})
