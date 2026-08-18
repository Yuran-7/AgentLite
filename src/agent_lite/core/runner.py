from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from agent_lite.core.bus.events import RunFinishedEvent, RunStartedEvent
from agent_lite.core.compact.compactor import Compactor
from agent_lite.core.config import AgentLiteConfig
from agent_lite.core.context import ExecutionContext
from agent_lite.core.events.bus import EventBus, EventHandler
from agent_lite.core.events.writer import EventAppender, EventWriter
from agent_lite.core.llm.base import LLMProvider
from agent_lite.core.llm.factory import create_llm_provider
from agent_lite.core.loop import AgentLoop
from agent_lite.core.mcp.server import McpServerManager
from agent_lite.core.memory.loader import load_agent_context, load_context_file
from agent_lite.core.permissions.manager import PermissionManager
from agent_lite.core.runs import new_run_id
from agent_lite.core.session.model import Session
from agent_lite.core.session.store import SessionStore
from agent_lite.core.subagent.registry import BackgroundTaskRegistry
from agent_lite.core.subagent.tool import AgentResultTool, SpawnAgentTool
from agent_lite.core.tools.builtin import (
    BrowserTool,
    ListDirTool,
    NoteSaveTool,
    ReadFileTool,
    ShellTool,
    UpdatePlanTool,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
)
from agent_lite.core.tools.builtin.browser_session import BrowserSessionManager
from agent_lite.core.tools.registry import ToolRegistry
from agent_lite.core.trace.provider import TracingProvider
from agent_lite.core.trace.writer import TraceWriter


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: AgentLiteConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        events_file: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None,
        browser_manager: BrowserSessionManager | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._events_file = events_file or Path("events.jsonl")
        self._trace = trace
        self._permission_manager = permission_manager
        self._mcp_manager = mcp_manager
        self._browser_manager = browser_manager or BrowserSessionManager(
            config.web.browser_idle_timeout_s
        )
        # 跨 run 共享的后台 subagent 任务注册表
        self._task_registry = BackgroundTaskRegistry()

    # 构建工具注册表，并按需注入计划和 SpawnAgentTool
    def _build_registry(
        self,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        session_id: str = "",
        workspace_root: Path | None = None,
        agent_context: str = "",
        tool_whitelist: list[str] | None = None,
    ) -> ToolRegistry:
        allowed: set[str] | None = (
            {
                "shell" if name == "bash" else name
                for name in tool_whitelist
            }
            if tool_whitelist is not None
            else None
        )

        def _ok(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        for t in [
            ReadFileTool(workspace_root),
            ShellTool(workspace_root),
            WriteFileTool(workspace_root),
            ListDirTool(workspace_root),
        ]:
            if _ok(t.name):
                registry.register(t)
        if self._config.web.enabled:
            for t in [WebSearchTool(self._config.web), WebFetchTool(self._config.web)]:
                if _ok(t.name):
                    registry.register(t)
            if self._config.web.browser_enabled and _ok("browser"):
                browser_scope = session_id or f"run:{run_id or id(registry)}"
                registry.register(
                    BrowserTool(
                        self._config.web,
                        session_manager=self._browser_manager,
                        session_id=browser_scope,
                        allow_user_handoff=(
                            session is not None and session.mode == "chat"
                        ),
                    )
                )
        if bus is not None and run_id is not None and _ok("update_plan"):
            registry.register(UpdatePlanTool(bus, run_id))
        if session is not None and store is not None and run_id is not None:
            note_tool = NoteSaveTool(store, session.id, run_id)
            if _ok(note_tool.name):
                registry.register(note_tool)
        if provider is not None and bus is not None and run_id is not None:
            if _ok("spawn_agent"):
                registry.register(
                    SpawnAgentTool(
                        provider=provider,
                        parent_bus=bus,
                        parent_run_id=run_id,
                        permission_manager=self._permission_manager,
                        max_steps=self._config.agent.max_steps,
                        task_registry=self._task_registry,
                        session_id=session_id,
                        workspace_root=workspace_root,
                        agent_context=agent_context,
                        web_config=self._config.web,
                        subagent_allowed_tools=self._config.agent.subagent_allowed_tools,
                        depth=0,
                    )
                )
            if _ok("agent_result"):
                registry.register(AgentResultTool(self._task_registry))
        if self._mcp_manager is not None:
            for mcp_tool in self._mcp_manager.get_tools():
                if _ok(mcp_tool.name):
                    registry.register(mcp_tool)
        return registry

    # 执行一次完整的 agent run（委托给 run_and_capture，忽略返回值）
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        await self.run_and_capture(goal, run_id=run_id)

    # 执行 agent run 并返回 RunOutcome（含最终文字结果）
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        # 1. 确定本次运行的唯一 run_id、目录，以及需要回放的会话上下文
        run_id = run_id or new_run_id()
        if session is not None and store is not None:
            session_dir = store.session_dir(session.id)
            history = store.read_messages(session.id)
            notes = store.read_notes(session.id)
        else:
            session_dir = self._events_file.parent
            history = [{"role": "user", "content": goal}]
            notes = ""

        workspace_root = (
            Path(session.workspace_root)
            if session is not None and session.workspace_root is not None
            else None
        )
        global_ctx = load_context_file(Path("~/.agentlite/context.md").expanduser())
        agent_ctx = load_agent_context(workspace_root)

        # 2. 建立本次 run 的局部事件总线，再桥接到全局总线供 TUI 实时订阅
        bus = EventBus()
        global_bus = self._bus
        if global_bus is not None:
            async def _bridge(event: BaseModel) -> None:
                await global_bus.publish(event)

            bus.subscribe(_bridge)
        for h in self._extra_handlers:
            bus.subscribe(h)

        # 3. 创建本次 run 的工作上下文；goal 或会话历史会成为初始消息
        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
            prefill_messages=history,
            session_notes=notes,
            global_context=global_ctx,
            agent_context=agent_ctx,
            workspace_root=workspace_root,
            system_prompt_override=system_prompt_override,
        )
        prefill_len = len(history)

        # 4. session 汇总写入根 events.jsonl；独立 run 写入单一事件文件
        async with AsyncExitStack() as stack:
            if session is not None and store is not None:
                EventAppender(store.events_file(session.id)).subscribe(bus)
            else:
                writer = await stack.enter_async_context(EventWriter(self._events_file))
                writer.subscribe(bus)
            # publish 会依次等待所有已订阅的 handler 处理该事件
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))

            cancelled = False
            registry: ToolRegistry | None = None
            try:
                provider: LLMProvider = self._provider or create_llm_provider(
                    self._config.llm
                )
                if self._trace is not None:
                    provider = TracingProvider(
                        provider,
                        self._trace,
                        include_payload=self._config.trace.include_llm_payload,
                    )
                session_id_str = session.id if session is not None else ""
                registry = self._build_registry(
                    session=session,
                    store=store,
                    run_id=run_id,
                    provider=provider,
                    bus=bus,
                    session_id=session_id_str,
                    workspace_root=workspace_root,
                    tool_whitelist=tool_whitelist,
                )
                compactor = Compactor(bus, session_dir, session_id_str)
                loop = AgentLoop(
                    provider, registry, bus,
                    permission_manager=self._permission_manager,
                    compactor=compactor,
                    compact_threshold=self._config.compaction.auto_threshold,
                    session_id=session_id_str,
                )
                await loop.run(context)
            except asyncio.CancelledError:
                cancelled = True
                if not context.is_done():
                    context.mark_failed("cancelled")
            except Exception:
                logging.getLogger(__name__).exception(
                    "agent run failed run_id=%s step=%d", run_id, context.step
                )
                if not context.is_done():
                    context.mark_failed("llm_error")
            finally:
                if registry is not None:
                    await registry.aclose()

            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )

        if session is not None and store is not None:
            store.append_messages(session.id, context.messages[prefill_len:], run_id=run_id)

        if cancelled:
            raise asyncio.CancelledError()

        return RunOutcome(
            status=context.status,
            result=context.result,
            reason=context.reason,
        )
