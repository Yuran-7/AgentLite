from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from agent_lite.core.agents.loader import AgentProfile, AgentProfileLoader
from agent_lite.core.bus.events import SubagentFinishedEvent, SubagentStartedEvent
from agent_lite.core.context import ExecutionContext
from agent_lite.core.events.bus import EventBus
from agent_lite.core.loop import AgentLoop
from agent_lite.core.runs import new_run_id
from agent_lite.core.subagent.registry import BackgroundTaskRegistry
from agent_lite.core.tools.base import BaseTool, ToolResult
from agent_lite.core.tools.builtin.bash import ShellTool
from agent_lite.core.tools.builtin.browser import BrowserTool
from agent_lite.core.tools.builtin.list_dir import ListDirTool
from agent_lite.core.tools.builtin.read_file import ReadFileTool
from agent_lite.core.tools.builtin.update_plan import UpdatePlanTool
from agent_lite.core.tools.builtin.web_fetch import WebFetchTool
from agent_lite.core.tools.builtin.web_search import WebSearchTool
from agent_lite.core.tools.builtin.write_file import WriteFileTool
from agent_lite.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from agent_lite.core.config import WebConfig
    from agent_lite.core.llm.base import LLMProvider
    from agent_lite.core.permissions.manager import PermissionManager

_profile_loader = AgentProfileLoader()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SpawnAgentParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str
    prompt: str
    run_in_background: bool = False
    subagent_type: str = ""


# 在隔离的冷启动上下文中派生子 agent，支持前台阻塞和后台并行两种模式
class SpawnAgentTool(BaseTool):
    name = "spawn_agent"
    description = (
        "Spawn an isolated sub-agent to handle a self-contained sub-task. "
        "The sub-agent starts with a clean context containing only the provided prompt — "
        "it does not inherit the current conversation history. "
        "Use run_in_background=true to run in parallel; retrieve result later with agent_result."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "3-5 word task description shown in progress display",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Complete task description including all context the sub-agent needs. "
                    "The sub-agent cannot see the parent conversation, so be explicit."
                ),
            },
            "run_in_background": {
                "type": "boolean",
                "description": "When true, returns immediately with a run_id; use agent_result to poll.",  # noqa: E501
            },
            "subagent_type": {
                "type": "string",
                "description": (
                    "Agent role profile name, such as debate-judge or metagpt-qa. "
                    "Leave empty for the default profile."
                ),
            },
        },
        "required": ["description", "prompt"],
    }
    params_model = SpawnAgentParams

    # 构造 SpawnAgentTool；depth=0 表示根 agent，最大允许嵌套深度为 2
    def __init__(
        self,
        provider: LLMProvider,
        parent_bus: EventBus,
        parent_run_id: str,
        permission_manager: PermissionManager | None,
        max_steps: int,
        task_registry: BackgroundTaskRegistry,
        session_id: str,
        workspace_root: Path | None = None,
        agent_context: str = "",
        web_config: WebConfig | None = None,
        subagent_allowed_tools: list[str] | None = None,
        depth: int = 0,
    ) -> None:
        self._provider = provider
        self._parent_bus = parent_bus
        self._parent_run_id = parent_run_id
        self._permission_manager = permission_manager
        self._max_steps = max_steps
        self._task_registry = task_registry
        self._session_id = session_id
        self._workspace_root = workspace_root
        self._agent_context = agent_context
        self._web_config = web_config
        self._subagent_allowed_tools = (
            {
                "shell" if name == "bash" else name
                for name in subagent_allowed_tools
            }
            if subagent_allowed_tools is not None
            else {
                "read_file",
                "shell",
                "write_file",
                "list_dir",
                "update_plan",
                "spawn_agent",
                "agent_result",
            }
        )
        self._depth = depth

    # 派生子 agent，前台时阻塞直到完成并返回结果，后台时立即返回 run_id
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SpawnAgentParams.model_validate(params)

        if self._depth >= 2:
            return ToolResult(
                content="Subagent nesting limit (2) reached; cannot spawn further subagents.",
                is_error=True,
                error_type="runtime_error",
            )

        profile: AgentProfile | None = None
        if p.subagent_type:
            profile = _profile_loader.load(p.subagent_type)
            if profile is None:
                return ToolResult(
                    content=f"Unknown or invalid subagent profile: {p.subagent_type}",
                    is_error=True,
                    error_type="schema_error",
                )

        child_run_id = new_run_id()
        child_context = ExecutionContext(
            run_id=child_run_id,
            goal=p.prompt,
            max_steps=self._max_steps,
            agent_context=self._agent_context,
            workspace_root=self._workspace_root,
            system_prompt_override=profile.system_prompt if profile else None,
        )

        child_bus = EventBus()

        # 将子 bus 所有事件桥接到父 bus，TUI 据此渲染嵌套进度
        async def _bridge(event: BaseModel) -> None:
            await self._parent_bus.publish(event)

        child_bus.subscribe(_bridge)

        child_registry = self._build_child_registry(child_bus, child_run_id, profile)
        child_loop = AgentLoop(
            self._provider,
            child_registry,
            child_bus,
            permission_manager=self._permission_manager,
            session_id=self._session_id,
        )

        await self._parent_bus.publish(
            SubagentStartedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                description=p.description,
                ts=_now(),
            )
        )

        if p.run_in_background:
            task: asyncio.Task[None] = asyncio.create_task(
                self._run_background(
                    child_loop,
                    child_context,
                    child_bus,
                    child_run_id,
                    child_registry,
                )
            )
            self._task_registry.register(child_run_id, task, child_context)
            return ToolResult(
                content=(
                    f"Subagent started in background. run_id={child_run_id}. "
                    f"Use agent_result(run_id='{child_run_id}') to retrieve result."
                )
            )

        try:
            await child_loop.run(child_context)
        finally:
            await child_registry.aclose()

        await self._parent_bus.publish(
            SubagentFinishedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                status=child_context.status,
                ts=_now(),
            )
        )

        if child_context.status == "success":
            return ToolResult(
                content=child_context.result or "Subagent completed with no text output."
            )
        return ToolResult(
            content=(
                child_context.result
                or f"Subagent failed (status={child_context.status}, reason={child_context.reason})"
            ),
            is_error=True,
            error_type="runtime_error",
        )

    # 后台任务协程：写事件文件，运行 loop，发布完成事件
    async def _run_background(
        self,
        loop: AgentLoop,
        context: ExecutionContext,
        bus: EventBus,
        run_id: str,
        registry: ToolRegistry,
    ) -> None:
        try:
            await loop.run(context)
        finally:
            await registry.aclose()
        await self._parent_bus.publish(
            SubagentFinishedEvent(
                run_id=run_id,
                parent_run_id=self._parent_run_id,
                status=context.status,
                ts=_now(),
            )
        )

    # 构造子 registry；基于角色配置过滤工具，深度允许时注册嵌套 SpawnAgentTool
    def _build_child_registry(
        self,
        child_bus: EventBus,
        child_run_id: str,
        profile: AgentProfile | None,
    ) -> ToolRegistry:
        profile_allowed: set[str] | None = (
            set(profile.allowed_tools) if profile is not None else None
        )

        def _allowed(name: str) -> bool:
            within_global_cap = name in self._subagent_allowed_tools
            within_profile = profile_allowed is None or name in profile_allowed
            return within_global_cap and within_profile

        registry = ToolRegistry()
        _all_tools = [
            ReadFileTool(self._workspace_root),
            ShellTool(self._workspace_root),
            WriteFileTool(self._workspace_root),
            ListDirTool(self._workspace_root),
        ]
        for t in _all_tools:
            if _allowed(t.name):
                registry.register(t)

        if self._web_config is not None and self._web_config.enabled:
            for t in [WebSearchTool(self._web_config), WebFetchTool(self._web_config)]:
                if _allowed(t.name):
                    registry.register(t)
            if self._web_config.browser_enabled and _allowed("browser"):
                registry.register(BrowserTool(self._web_config))

        if _allowed("update_plan"):
            registry.register(UpdatePlanTool(child_bus, child_run_id))

        if self._depth < 1:
            nested = SpawnAgentTool(
                provider=self._provider,
                parent_bus=child_bus,
                parent_run_id=child_run_id,
                permission_manager=self._permission_manager,
                max_steps=self._max_steps,
                task_registry=self._task_registry,
                session_id=self._session_id,
                workspace_root=self._workspace_root,
                agent_context=self._agent_context,
                web_config=self._web_config,
                subagent_allowed_tools=sorted(self._subagent_allowed_tools),
                depth=self._depth + 1,
            )
            if _allowed("spawn_agent"):
                registry.register(nested)
            if _allowed("agent_result"):
                registry.register(AgentResultTool(self._task_registry))

        return registry


class AgentResultParams(BaseModel):
    run_id: str
    timeout_seconds: float = Field(default=0, ge=0, le=60)


# 查询后台 subagent 的执行状态和最终结果
class AgentResultTool(BaseTool):
    name = "agent_result"
    description = (
        "Retrieve the result of a background sub-agent previously started with spawn_agent. "
        "Optionally wait for it to finish, returning immediately when it completes. "
        "Returns 'still running' if the sub-agent has not completed before the timeout."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The run_id returned by spawn_agent(run_in_background=true)",
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 0,
                "maximum": 60,
                "default": 0,
                "description": (
                    "Maximum seconds to wait for completion. Returns immediately when the "
                    "sub-agent finishes; 0 checks status without waiting."
                ),
            },
        },
        "required": ["run_id"],
    }
    params_model = AgentResultParams

    # 初始化，持有共享的后台任务注册表
    def __init__(self, task_registry: BackgroundTaskRegistry) -> None:
        self._task_registry = task_registry

    # 查询指定 run_id 的后台任务状态，返回结果或错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = AgentResultParams.model_validate(params)
        entry = self._task_registry.get(p.run_id)
        if entry is None:
            return ToolResult(
                content=f"Unknown run_id: {p.run_id}. Only background subagents can be queried.",
                is_error=True,
                error_type="runtime_error",
            )
        task, context = entry
        if not task.done():
            if p.timeout_seconds == 0:
                return ToolResult(content="still running")
            done, _ = await asyncio.wait({task}, timeout=p.timeout_seconds)
            if not done:
                return ToolResult(content="still running")
        if task.cancelled():
            return ToolResult(
                content="Subagent was cancelled.", is_error=True, error_type="runtime_error"
            )
        exc = task.exception()
        if exc is not None:
            return ToolResult(
                content=f"Subagent raised an exception: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=context.result or "Subagent completed with no text result.")
