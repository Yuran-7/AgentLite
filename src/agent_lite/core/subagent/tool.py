from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from agent_lite.core.agents.loader import AgentDefinition, AgentRegistry
from agent_lite.core.bus.events import SubagentFinishedEvent, SubagentStartedEvent
from agent_lite.core.context import ExecutionContext
from agent_lite.core.events.bus import EventBus
from agent_lite.core.loop import AgentLoop
from agent_lite.core.runs import new_run_id
from agent_lite.core.subagent.registry import SubagentTaskManager
from agent_lite.core.tools.base import BaseTool, ToolResult
from agent_lite.core.tools.builtin.bash import ShellTool
from agent_lite.core.tools.builtin.cosil_localize import CosilLocalizeTool
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


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SpawnAgentParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    prompt: str
    run_in_background: bool = False
    subagent_type: str = "general-purpose"


class SpawnAgentTool(BaseTool):
    name = "spawn_agent"
    params_model = SpawnAgentParams

    def __init__(
        self,
        provider: LLMProvider,
        parent_bus: EventBus,
        parent_run_id: str,
        permission_manager: PermissionManager | None,
        max_steps: int,
        task_manager: SubagentTaskManager,
        session_id: str,
        workspace_root: Path | None = None,
        agent_context: str = "",
        web_config: WebConfig | None = None,
        subagent_allowed_tools: list[str] | None = None,
        depth: int = 0,
        agent_registry: AgentRegistry | None = None,
        provider_factory: Callable[[str], LLMProvider] | None = None,
        extra_tools: list[BaseTool] | None = None,
    ) -> None:
        self._provider = provider
        self._provider_factory = provider_factory
        self._parent_bus = parent_bus
        self._parent_run_id = parent_run_id
        self._permission_manager = permission_manager
        self._max_steps = max_steps
        self._task_manager = task_manager
        self._session_id = session_id
        self._workspace_root = workspace_root
        self._agent_context = agent_context
        self._web_config = web_config
        configured_tools = (
            subagent_allowed_tools
            if subagent_allowed_tools is not None
            else [
                "read_file",
                "shell",
                "write_file",
                "list_dir",
                "update_plan",
                "spawn_agent",
            ]
        )
        self._subagent_allowed_tools = {
            "shell" if name == "bash" else name
            for name in configured_tools
        }
        self._depth = depth
        self._agent_registry = agent_registry or AgentRegistry(workspace_root)
        self._extra_tools = extra_tools or []

        def describe_agent(agent: AgentDefinition) -> str:
            tools = self._agent_registry.effective_tools(
                agent, self._subagent_allowed_tools
            )
            tool_names = ", ".join(tools) or "none"
            return f"- {agent.name}: {agent.description} (tools: {tool_names})"

        listing = "\n".join(
            describe_agent(agent) for agent in self._agent_registry.list_all()
        )
        self.description = (
            "Launch an isolated subagent for a self-contained task. The subagent starts with only "
            "the supplied prompt. Available agent types:\n"
            f"{listing}\n"
            "Omit subagent_type to use general-purpose. For independent work, set "
            "run_in_background=true; it returns an output_file and completion arrives "
            "automatically "
            "as a task-notification. Do not poll, sleep, or repeatedly read the output file."
        )
        names = [agent.name for agent in self._agent_registry.list_all()]
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short 3-5 word task description",
                },
                "prompt": {
                    "type": "string",
                    "description": "Complete task brief; the subagent cannot see parent history",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Run independently and receive an automatic completion notification"
                    ),
                },
                "subagent_type": {
                    "type": "string",
                    "enum": names,
                    "default": "general-purpose",
                    "description": "Specialized agent type",
                },
            },
            "required": ["description", "prompt"],
        }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SpawnAgentParams.model_validate(params)
        if self._depth >= 2:
            return ToolResult(
                content="Subagent nesting limit (2) reached; cannot spawn further subagents.",
                is_error=True,
                error_type="runtime_error",
            )

        definition = self._agent_registry.get(p.subagent_type)
        if definition is None:
            available = ", ".join(agent.name for agent in self._agent_registry.list_all())
            return ToolResult(
                content=f"Unknown subagent type: {p.subagent_type}. Available agents: {available}",
                is_error=True,
                error_type="schema_error",
            )

        provider = self._provider
        if definition.model != "inherit":
            if self._provider_factory is None:
                return ToolResult(
                    content=f"Agent model override is unavailable: {definition.model}",
                    is_error=True,
                    error_type="runtime_error",
                )
            provider = self._provider_factory(definition.model)

        child_run_id = new_run_id()
        child_context = ExecutionContext(
            run_id=child_run_id,
            goal=p.prompt,
            max_steps=min(self._max_steps, definition.max_turns or self._max_steps),
            agent_context=self._agent_context,
            workspace_root=self._workspace_root,
            system_prompt_override=definition.system_prompt,
        )
        child_bus = EventBus()

        async def _bridge(event: BaseModel) -> None:
            await self._parent_bus.publish(event)

        child_bus.subscribe(_bridge)
        child_registry = self._build_child_registry(
            child_bus, child_run_id, definition, provider
        )
        child_loop = AgentLoop(
            provider,
            child_registry,
            child_bus,
            permission_manager=self._permission_manager,
            session_id=self._session_id,
            task_manager=self._task_manager,
        )
        await self._parent_bus.publish(
            SubagentStartedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                description=p.description,
                ts=_now(),
            )
        )

        if p.run_in_background or definition.background:
            task = asyncio.create_task(
                self._run_background(
                    child_loop, child_context, child_registry
                ),
                name=f"subagent-{child_run_id}",
            )
            record = self._task_manager.register(
                task_id=child_run_id,
                task=task,
                context=child_context,
                session_id=self._session_id,
                owner_run_id=self._parent_run_id,
                agent_type=definition.name,
                description=p.description,
            )
            return ToolResult(
                content=(
                    "status: async_launched\n"
                    f"task_id: {child_run_id}\n"
                    f"output_file: {record.output_file}\n"
                    "You will be notified automatically when it completes."
                )
            )

        await self._task_manager.activate_run(child_run_id)
        cancelled = False
        try:
            await child_loop.run(child_context)
        except asyncio.CancelledError:
            cancelled = True
            if not child_context.is_done():
                child_context.mark_failed("cancelled")
        finally:
            await self._task_manager.deactivate_run(child_run_id)
            await child_registry.aclose()
        await self._publish_finished(child_run_id, child_context.status)
        if cancelled:
            raise asyncio.CancelledError()
        if child_context.status == "success":
            return ToolResult(
                content=child_context.result or "Subagent completed with no text output."
            )
        return ToolResult(
            content=child_context.result or (
                f"Subagent failed (status={child_context.status}, reason={child_context.reason})"
            ),
            is_error=True,
            error_type="runtime_error",
        )

    async def _run_background(
        self,
        loop: AgentLoop,
        context: ExecutionContext,
        registry: ToolRegistry,
    ) -> None:
        await self._task_manager.activate_run(context.run_id)
        status = "failed"
        error = ""
        try:
            await loop.run(context)
            status = "completed" if context.status == "success" else "failed"
            error = (context.reason or "") if status == "failed" else ""
        except asyncio.CancelledError:
            status = "cancelled"
            error = "cancelled"
            if not context.is_done():
                context.mark_failed("cancelled")
        except Exception as exc:
            status = "failed"
            error = str(exc)
            if not context.is_done():
                context.mark_failed("runtime_error")
        finally:
            await self._task_manager.deactivate_run(context.run_id)
            await registry.aclose()
            try:
                await self._publish_finished(context.run_id, context.status)
            finally:
                await self._task_manager.finish(
                    context.run_id,
                    status=status,
                    result=context.result,
                    error=error,
                )

    async def _publish_finished(self, run_id: str, status: str) -> None:
        await self._parent_bus.publish(
            SubagentFinishedEvent(
                run_id=run_id,
                parent_run_id=self._parent_run_id,
                status=status,
                ts=_now(),
            )
        )

    def _build_child_registry(
        self,
        child_bus: EventBus,
        child_run_id: str,
        definition: AgentDefinition,
        provider: LLMProvider,
    ) -> ToolRegistry:
        allowed: set[str] = set(
            self._agent_registry.effective_tools(definition, self._subagent_allowed_tools)
        )
        registry = ToolRegistry()
        tools: list[BaseTool] = [
            ReadFileTool(self._workspace_root),
            ShellTool(self._workspace_root),
            WriteFileTool(self._workspace_root),
            ListDirTool(self._workspace_root),
        ]
        if self._workspace_root is not None:
            tools.append(CosilLocalizeTool(provider, child_bus, child_run_id, self._workspace_root))
        if self._web_config is not None and self._web_config.enabled:
            tools.extend([WebSearchTool(self._web_config), WebFetchTool(self._web_config)])
        if "update_plan" in allowed:
            tools.append(UpdatePlanTool(child_bus, child_run_id))
        for tool in tools:
            if tool.name in allowed:
                registry.register(tool)
        for tool in self._extra_tools:
            if tool.name in allowed:
                registry.register(tool)

        if self._depth < 1 and "spawn_agent" in allowed:
            registry.register(
                SpawnAgentTool(
                    provider=provider,
                    provider_factory=self._provider_factory,
                    parent_bus=child_bus,
                    parent_run_id=child_run_id,
                    permission_manager=self._permission_manager,
                    max_steps=self._max_steps,
                    task_manager=self._task_manager,
                    session_id=self._session_id,
                    workspace_root=self._workspace_root,
                    agent_context=self._agent_context,
                    web_config=self._web_config,
                    subagent_allowed_tools=sorted(self._subagent_allowed_tools),
                    depth=self._depth + 1,
                    agent_registry=self._agent_registry,
                    extra_tools=self._extra_tools,
                )
            )
        return registry
