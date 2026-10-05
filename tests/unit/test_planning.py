from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError
from textual.app import App
from textual.widgets import Input, Select

from agent_lite.core.bus.commands import SessionCollaborationCommand
from agent_lite.core.config import AgentLiteConfig
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.types import ToolCallBlock
from agent_lite.core.planning import RequestInputParams, RequestUserInputTool, UserInputManager
from agent_lite.core.runner import AgentRunner
from agent_lite.core.session.model import Session
from agent_lite.core.tools.invocation import invoke_tool
from agent_lite.core.tools.registry import ToolRegistry
from agent_lite.tui.plan_input import PlanInputScreen

QUESTIONS = {"questions": [{
    "id": "scope", "header": "范围", "question": "选择实现范围？",
    "options": [{"label": "完整 (Recommended)", "description": "实现全部流程"},
                {"label": "最小", "description": "仅实现核心"}],
}]}


# 功能：问题等待显式回答，拒绝跨会话、漏答和重复提交。
# 设计：运行真实工具并由事件总线捕获请求标识，验证答案返回模型的完整结构。
async def test_input_waits_and_validates_answers() -> None:
    manager = UserInputManager()
    bus = EventBus()
    events = []

    # 捕获问题事件并保留取消后的终态。
    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    task = asyncio.create_task(RequestUserInputTool(manager, bus, "run", "session").invoke(
        QUESTIONS
    ))
    await asyncio.sleep(0)
    request = next(iter(manager.pending))
    assert not task.done()
    with pytest.raises(ValueError):
        manager.respond("other-session", request, {"scope": "最小"})
    with pytest.raises(ValueError):
        manager.respond("session", request, {})
    manager.respond("session", request, {"scope": "自定义方案"})
    with pytest.raises(ValueError):
        manager.respond("session", request, {"scope": "最小"})
    result = await task
    assert json.loads(result.content) == {"answers": {"scope": {"answers": ["自定义方案"]}}}
    assert not manager.pending
    assert [getattr(event, "type") for event in events] == [
        "user_input.requested", "user_input.resolved"
    ]


# 功能：取消运行释放所有问答等待，不保留可回答的过期请求。
# 设计：取消真实工具协程，确认 finally 清理且没有后台等待泄漏。
async def test_cancel_input_cleans_pending() -> None:
    manager = UserInputManager()
    task = asyncio.create_task(RequestUserInputTool(
        manager, EventBus(), "run", "session"
    ).invoke(QUESTIONS))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager.pending


# 功能：计划模式在工具注册层阻断文件写入、任意命令和子代理。
# 设计：构建真实运行注册表并往返会话元数据，覆盖旧会话默认值与模式持久化。
def test_plan_mode_limits_tools_and_persists() -> None:
    session = Session("session", "chat", "active", "", "", "", collaboration_mode="plan")
    runner = AgentRunner(AgentLiteConfig(), input_manager=UserInputManager())
    registry = runner._build_registry(session=session, bus=EventBus(), run_id="run")
    assert registry.get("read_file") is not None
    assert registry.get("request_user_input") is not None
    for name in ("write_file", "edit_file", "shell", "spawn_agent", "update_plan"):
        assert registry.get(name) is None
    assert Session.from_dict(session.to_dict()).collaboration_mode == "plan"
    old = session.to_dict()
    old.pop("collaboration_mode")
    assert Session.from_dict(old).collaboration_mode == "default"
    with pytest.raises(ValidationError):
        SessionCollaborationCommand(session_id="session", mode="invalid")


# 功能：拒绝重复问题标识及超过限制的问题组。
# 设计：基于真实参数模型校验边界，防止回答映射被重复键覆盖。
def test_questions_have_unique_ids() -> None:
    with pytest.raises(ValidationError):
        RequestInputParams.model_validate({"questions": QUESTIONS["questions"] * 2})


# 功能：问答等待不受普通工具超时限制，完整答案通过正常调用链返回。
# 设计：将超时设置为零并走真实工具调用包装器，排除直接调用绕过超时的假阳性。
async def test_input_ignores_execution_timeout() -> None:
    manager = UserInputManager()
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(RequestUserInputTool(manager, bus, "run", "session"))
    task = asyncio.create_task(invoke_tool(
        registry, ToolCallBlock(id="call", name="request_user_input", input=QUESTIONS),
        bus, "run", timeout=0,
    ))
    for _ in range(10):
        await asyncio.sleep(0)
        if manager.pending:
            break
    manager.respond("session", next(iter(manager.pending)), {"scope": "最小"})
    assert not (await task).is_error


# 功能：终端弹窗提供默认选项和自由输入，并显式提交完整答案。
# 设计：启动真实 Textual 测试应用并点击提交，验证弹窗渲染与回调数据。
async def test_tui_question_modal() -> None:
    app: App[None] = App()
    answers: list[dict[str, str] | None] = []
    async with app.run_test() as pilot:
        await app.push_screen(PlanInputScreen(QUESTIONS["questions"]), answers.append)
        await pilot.pause()
        assert not answers
        app.screen.query_one("#choice-0", Select).value = ""
        app.screen.query_one("#text-0", Input).value = "自定义方案"
        await pilot.click("#submit")
        await pilot.pause()
        assert answers == [{"scope": "自定义方案"}]
