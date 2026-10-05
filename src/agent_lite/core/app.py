from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import agent_lite
from agent_lite.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    CoreKeepAliveCommand,
    CoreKeepAliveResult,
    CoreShutdownCommand,
    CoreShutdownResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    FileHistoryDiffCommand,
    FileHistoryDiffResult,
    FileHistoryListCommand,
    FileHistoryListResult,
    FileHistoryRestoreCommand,
    FileHistoryRestoreResult,
    FrontendHeartbeatCommand,
    FrontendLeaseResult,
    FrontendRegisterCommand,
    FrontendUnregisterCommand,
    FrontendUnregisterResult,
    McpListCommand,
    McpManageCommand,
    MemoryDeleteCommand,
    MemoryDeleteResult,
    MemoryListCommand,
    MemoryListResult,
    MemorySearchCommand,
    MemorySearchResult,
    ModelListCommand,
    PermissionRespondCommand,
    PermissionRespondResult,
    PongResult,
    SessionCancelCommand,
    SessionCancelResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCollaborationCommand,
    SessionCompactCommand,
    SessionCompactResult,
    SessionCreateCommand,
    SessionCreateResult,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionListCommand,
    SessionListResult,
    SessionPermissionModeCommand,
    SessionReasoningCommand,
    SessionRenameCommand,
    SessionResumeCommand,
    SessionResumeResult,
    SessionSendMessageCommand,
    SessionSendMessageResult,
    SessionSetMemoryCommand,
    SessionSetMemoryResult,
    SessionSetModelCommand,
    SessionSetStatsCommand,
    SessionSetStatsResult,
    SessionSetWorkspaceCommand,
    SessionSetWorkspaceResult,
    SessionSummary,
    SkillListCommand,
    SkillListResult,
    SkillSummary,
    UserInputRespondCommand,
)
from agent_lite.core.bus.envelope import INVALID_PARAMS, HandlerError, JsonRpcNotification
from agent_lite.core.config import AgentLiteConfig, get_config
from agent_lite.core.events.bus import EventBus
from agent_lite.core.lifecycle import FrontendLifecycle
from agent_lite.core.llm.factory import DeferredProvider, create_llm_provider
from agent_lite.core.llm.images import ImageAttachment
from agent_lite.core.llm.reasoning import supported_efforts, with_reasoning
from agent_lite.core.llm.settings import model_settings, resolve_model
from agent_lite.core.logging_setup import setup_logging
from agent_lite.core.mcp.server import McpServerManager
from agent_lite.core.mcp.settings import parse_server
from agent_lite.core.memory import MemoryStore
from agent_lite.core.permissions.manager import PermissionManager
from agent_lite.core.permissions.storage import load_policy_file
from agent_lite.core.planning import UserInputManager
from agent_lite.core.runner import AgentRunner
from agent_lite.core.runs import new_run_id
from agent_lite.core.session import SessionManager, SessionStore
from agent_lite.core.skills.loader import SkillLoader
from agent_lite.core.subagent.registry import SubagentTaskManager
from agent_lite.core.tools.file_operations import recover_file_histories
from agent_lite.core.trace.record import TraceRecord
from agent_lite.core.trace.writer import TraceWriter
from agent_lite.core.transport.ipc_broadcaster import IpcEventBroadcaster
from agent_lite.core.transport.socket_server import SocketServer, get_connection_writer

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class _RunningRun:
    session_id: str
    task: asyncio.Task[str]


class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: AgentLiteConfig | None = None
        self._running_runs: dict[str, _RunningRun] = {}
        self._input_manager = UserInputManager()
        self._sessions: SessionManager | None = None
        self._permission_manager: PermissionManager | None = None
        self._mcp_manager: McpServerManager | None = None
        self._shutdown: asyncio.Event | None = None
        self._sessions_root: Path | None = None
        self._memory_store: MemoryStore | None = None
        self._task_manager: SubagentTaskManager | None = None
        self._lifecycle: FrontendLifecycle | None = None
        self._mcp_lock = asyncio.Lock()
        self._mcp_changing = False
        self._server: SocketServer | None = None

    # 将前端租约绑定到当前 TCP 连接并返回续约规则
    async def _frontend_register(self, params: dict[str, Any]) -> FrontendLeaseResult:
        cmd = FrontendRegisterCommand.model_validate(params)
        assert self._lifecycle is not None
        self._lifecycle.register(get_connection_writer(), cmd.client)
        return FrontendLeaseResult(
            managed=self._lifecycle.managed, frontend_counts=self._lifecycle.connection_counts(),
        )

    # 续约当前连接，其他连接不能代替失效前端保持存活
    async def _frontend_heartbeat(self, params: dict[str, Any]) -> FrontendLeaseResult:
        FrontendHeartbeatCommand.model_validate(params)
        assert self._lifecycle is not None
        self._lifecycle.heartbeat(get_connection_writer())
        return FrontendLeaseResult(
            managed=self._lifecycle.managed, frontend_counts=self._lifecycle.connection_counts(),
        )

    # 前端退出时主动注销，异常退出则由连接关闭或心跳超时兜底
    async def _frontend_unregister(self, params: dict[str, Any]) -> FrontendUnregisterResult:
        FrontendUnregisterCommand.model_validate(params)
        assert self._lifecycle is not None
        self._lifecycle.unregister(get_connection_writer())
        return FrontendUnregisterResult()

    # 显式手动启动时保留现有 core，将其提升为常驻模式
    async def _keep_alive_handler(self, params: dict[str, Any]) -> CoreKeepAliveResult:
        CoreKeepAliveCommand.model_validate(params)
        assert self._lifecycle is not None
        self._lifecycle.keep_alive()
        return CoreKeepAliveResult()

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=agent_lite.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

    # 处理 core.shutdown 请求，在响应发出后触发守护进程的跨平台优雅退出
    async def _shutdown_handler(self, params: dict[str, Any]) -> CoreShutdownResult:
        CoreShutdownCommand.model_validate(params)
        assert self._shutdown is not None
        if self._lifecycle is not None:
            self._lifecycle.close()
        asyncio.get_running_loop().call_later(0.05, self._shutdown.set)
        return CoreShutdownResult()

    # 将 EventBus 事件写入 trace（作为 EventBus 订阅者）
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict,
            )
        )

    # 启动一次 agent run：异步创建 AgentRunner 并立即返回 run_id
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._sessions is not None
        cmd = AgentRunCommand.model_validate(params)
        session = await self._sessions.create(
            mode="one_shot",
            title=cmd.goal[:40],
            workspace_root=cmd.workspace_root,
            permission_mode=cmd.permission_mode,
        )
        run_id = new_run_id()
        self._start_session_run(session.id, cmd.goal, run_id)
        return AgentRunResult(run_id=run_id)

    # 创建独立的业务 Task 并按 run_id 登记，使 RPC Task 可以独立处理取消结果
    def _start_session_run(
        self,
        session_id: str,
        content: str,
        run_id: str,
        images: list[ImageAttachment] | None = None,
    ) -> asyncio.Task[str]:
        if self._mcp_changing:
            raise HandlerError(INVALID_PARAMS, "MCP 正在更新，请稍后发送任务")
        assert self._sessions is not None
        task = asyncio.create_task(
            self._sessions.send_message(
                session_id, content, run_id=run_id, **({"images": images} if images else {}),
            ),
            name=f"agent-run-{run_id}",
        )
        self._running_runs[run_id] = _RunningRun(session_id=session_id, task=task)

        def _discard(_completed: asyncio.Future[str]) -> None:
            self._discard_running_run(run_id, task)

        task.add_done_callback(_discard)
        return task

    # 仅在登记的仍是同一 Task 时删除，避免延迟回调误删新 run
    def _discard_running_run(self, run_id: str, task: asyncio.Task[str]) -> None:
        running = self._running_runs.get(run_id)
        if running is not None and running.task is task:
            self._running_runs.pop(run_id, None)

    # 创建 chat 或 one_shot session，并返回 session_id
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        cmd = SessionCreateCommand.model_validate(params)
        session = await self._sessions.create(
            mode=cmd.mode,
            title=cmd.title,
            workspace_root=cmd.workspace_root,
            permission_mode=cmd.permission_mode,
        )
        return SessionCreateResult(
            permission_mode=session.permission_mode,
            session_id=session.id,
            status=session.status,
            workspace_root=session.workspace_root,
            memory_generate_enabled=session.memory_generate_enabled,
            memory_use_enabled=session.memory_use_enabled,
        )

    # 列出磁盘中可恢复的 chat session，可选按工作区过滤
    async def _session_list_handler(self, params: dict[str, Any]) -> SessionListResult:
        assert self._sessions is not None
        cmd = SessionListCommand.model_validate(params)
        sessions = self._sessions.list_sessions(cmd.workspace_root)
        return SessionListResult(
            sessions=[
                SessionSummary(
                    session_id=session.id,
                    title=session.title,
                    status=session.status,
                    workspace_root=session.workspace_root,
                    created_at=session.created_at,
                    updated_at=session.updated_at,
                )
                for session in sessions
            ]
        )

    # 恢复指定 chat session，并返回 TUI 切换所需的完整状态
    async def _session_resume_handler(self, params: dict[str, Any]) -> SessionResumeResult:
        assert self._sessions is not None
        cmd = SessionResumeCommand.model_validate(params)
        session = await self._sessions.resume(cmd.session_id, cmd.workspace_root)
        return SessionResumeResult(
            model_id=session.model_id,
            reasoning_effort=session.reasoning_effort,
            collaboration_mode=session.collaboration_mode,
            permission_mode=session.permission_mode,
            session_id=session.id,
            title=session.title,
            status=session.status,
            workspace_root=session.workspace_root,
            memory_generate_enabled=session.memory_generate_enabled,
            memory_use_enabled=session.memory_use_enabled,
            stats=session.ui_stats,
        )

    # 返回模型元数据；从不向前端发送密钥。
    async def _model_list_handler(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self._config is not None
        cmd = ModelListCommand.model_validate(params)
        try:
            settings = model_settings(cmd.workspace_root)
            settings["fallbackModel"] = {
                "id": "", "name": self._config.llm.default_model,
                "model": self._config.llm.default_model, "protocol": self._config.llm.protocol,
                "reasoningEffort": self._config.llm.reasoning_effort,
            }
            return settings
        except ValueError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    async def _mcp_list_handler(self, params: dict[str, Any]) -> dict[str, Any]:
        McpListCommand.model_validate(params)
        assert self._mcp_manager is not None
        return self._mcp_manager.snapshot()

    # 返回当前工作区可见的 skill 元数据，供各前端展示和调用。
    async def _skill_list_handler(self, params: dict[str, Any]) -> SkillListResult:
        cmd = SkillListCommand.model_validate(params)
        skills = SkillLoader().list_all_skills(cmd.workspace_root)
        return SkillListResult(skills=[SkillSummary(
            name=skill.name, description=skill.description, source=skill.source,
            path=str(skill.path) if skill.path else "",
        ) for skill in skills])

    async def _mcp_manage_handler(self, params: dict[str, Any]) -> dict[str, Any]:
        cmd = McpManageCommand.model_validate(params)
        assert self._mcp_manager is not None
        async with self._mcp_lock:
            if cmd.action != "configure" and (
                any(not run.task.done() for run in self._running_runs.values())
                or self._task_manager is not None and self._task_manager.has_running_tasks
            ):
                raise HandlerError(INVALID_PARAMS, "有任务正在运行，请结束任务后管理 MCP")
            self._mcp_changing = cmd.action != "configure"
            try:
                if cmd.action == "reload":
                    await self._mcp_manager.reload()
                elif cmd.action == "reconnect":
                    await self._mcp_manager.reconnect(cmd.name)
                elif cmd.action == "set_enabled":
                    await self._mcp_manager.set_enabled(cmd.name, cmd.enabled)
                elif cmd.action == "add":
                    await self._mcp_manager.add(parse_server(cmd.server))
                elif cmd.action == "configure":
                    self._mcp_manager.prepare_settings()
                return self._mcp_manager.snapshot()
            except (ValueError, OSError) as exc:
                raise HandlerError(INVALID_PARAMS, str(exc)) from exc
            finally:
                self._mcp_changing = False

    async def _session_rename_handler(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self._sessions is not None
        cmd = SessionRenameCommand.model_validate(params)
        title = cmd.title.strip()
        if not title:
            raise HandlerError(INVALID_PARAMS, "Session title cannot be empty")
        session = await self._sessions.update_metadata(cmd.session_id, title=title)
        return {"title": session.title}

    async def _session_model_handler(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self._sessions is not None
        assert self._config is not None
        cmd = SessionSetModelCommand.model_validate(params)
        session = self._sessions._get_session(cmd.session_id)
        try:
            config = resolve_model(self._config.llm, cmd.model_id, session.workspace_root)
        except ValueError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        effort = session.reasoning_effort
        if effort not in ("", *supported_efforts(config.default_model, config.protocol)):
            effort = ""
        await self._sessions.update_metadata(
            cmd.session_id, model_id=cmd.model_id, reasoning_effort=effort
        )
        return {"model_id": session.model_id, "reasoning_effort": session.reasoning_effort}

    async def _session_reasoning_handler(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self._sessions is not None and self._config is not None
        cmd = SessionReasoningCommand.model_validate(params)
        session = self._sessions._get_session(cmd.session_id)
        try:
            config = resolve_model(self._config.llm, session.model_id, session.workspace_root)
            supported = bool(supported_efforts(config.default_model, config.protocol))
            if cmd.effort is not None:
                if cmd.effort and not supported:
                    raise ValueError("当前模型不支持推理强度设置")
                with_reasoning(config, cmd.effort)
                await self._sessions.update_metadata(cmd.session_id, reasoning_effort=cmd.effort)
            return {
                "supported": supported,
                "efforts": supported_efforts(config.default_model, config.protocol)
                if supported else [],
                "effort": session.reasoning_effort,
                "effective_effort": session.reasoning_effort or config.reasoning_effort,
                "model": config.default_model,
            }
        except ValueError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 为已有且尚未绑定工作区的 session 设置工作区
    # 读取或切换协作模式，忙碌会话由元数据锁拒绝修改。
    async def _session_collaboration_handler(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self._sessions is not None
        cmd = SessionCollaborationCommand.model_validate(params)
        if cmd.permission_mode is not None and cmd.mode is None:
            raise HandlerError(INVALID_PARAMS, "permission_mode requires collaboration mode")
        session = self._sessions._get_session(cmd.session_id)
        if cmd.mode is not None:
            session = await self._sessions.update_metadata(
                cmd.session_id, collaboration_mode=cmd.mode, permission_mode=cmd.permission_mode
            )
        return {"mode": session.collaboration_mode, "permission_mode": session.permission_mode}

    # 读取或切换会话权限模式，运行中的会话也可更新。
    async def _session_permission_mode_handler(self, params: dict[str, Any]) -> dict[str, Any]:
        assert self._sessions is not None
        cmd = SessionPermissionModeCommand.model_validate(params)
        session = self._sessions._get_session(cmd.session_id)
        if cmd.mode is not None:
            session = await self._sessions.set_permission_mode(cmd.session_id, cmd.mode)
        return {"mode": session.permission_mode}

    # 接收结构化问题答案并恢复当前模型运行。
    async def _user_input_handler(self, params: dict[str, Any]) -> dict[str, Any]:
        cmd = UserInputRespondCommand.model_validate(params)
        try:
            pending = self._input_manager.pending.get(cmd.request_id)
            self._input_manager.respond(cmd.session_id, cmd.request_id, cmd.answers)
            if pending is not None and self._sessions is not None:
                questions = {q.id: q.question for q in pending[1].questions}
                self._sessions._store.append_permission_user(
                    cmd.session_id,
                    "\n".join(
                        f"代理问题（仅上下文）：{questions[key]}\n用户回答：{answer}"
                        for key, answer in cmd.answers.items()
                    ),
                )
        except ValueError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return {"ok": True}

    # 为会话绑定工作区。
    async def _session_set_workspace_handler(
        self, params: dict[str, Any]
    ) -> SessionSetWorkspaceResult:
        assert self._sessions is not None
        cmd = SessionSetWorkspaceCommand.model_validate(params)
        workspace_root = await self._sessions.set_workspace(
            cmd.session_id, cmd.workspace_root
        )
        return SessionSetWorkspaceResult(workspace_root=workspace_root)

    # 向 session 发送消息；RPC Task 等待独立业务 Task，取消后仍能正常回包
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        cmd = SessionSendMessageCommand.model_validate(params)
        run_id = new_run_id()
        task = self._start_session_run(cmd.session_id, cmd.content, run_id, cmd.images)
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
        return SessionSendMessageResult(run_id=run_id)

    # 按 run_id 取消当前 Agent 运行，不关闭 Session、TUI 或 Core
    async def _session_cancel_handler(self, params: dict[str, Any]) -> SessionCancelResult:
        cmd = SessionCancelCommand.model_validate(params)
        running = self._running_runs.get(cmd.run_id)
        if running is None or running.task.done():
            return SessionCancelResult(run_id=cmd.run_id, accepted=False)
        if running.session_id != cmd.session_id:
            return SessionCancelResult(run_id=cmd.run_id, accepted=False)
        if self._permission_manager is not None:
            self._permission_manager.cancel_session(cmd.session_id, reason="run_cancelled")
        running.task.cancel()
        return SessionCancelResult(run_id=cmd.run_id, accepted=True)

    # 返回 session 的完整内部 messages 历史
    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        cmd = SessionGetHistoryCommand.model_validate(params)
        messages = await self._sessions.get_history(cmd.session_id)
        return SessionGetHistoryResult(
            messages=messages, last_usage=await self._sessions.last_usage(cmd.session_id),
        )

    # 修改当前 session 的 memory 检索和候选生成开关
    async def _session_set_memory_handler(self, params: dict[str, Any]) -> SessionSetMemoryResult:
        assert self._sessions is not None
        cmd = SessionSetMemoryCommand.model_validate(params)
        session = await self._sessions.set_memory(
            cmd.session_id,
            generate_enabled=cmd.generate_enabled,
            use_enabled=cmd.use_enabled,
        )
        return SessionSetMemoryResult(
            generate_enabled=session.memory_generate_enabled,
            use_enabled=session.memory_use_enabled,
        )

    # 持久化 TUI 的 session 统计快照，不改变会话的最后聊天时间
    async def _session_set_stats_handler(self, params: dict[str, Any]) -> SessionSetStatsResult:
        assert self._sessions is not None
        cmd = SessionSetStatsCommand.model_validate(params)
        stats = await self._sessions.set_ui_stats(cmd.session_id, cmd.stats)
        return SessionSetStatsResult(stats=stats)

    # 搜索当前调用者可见的长期记忆
    async def _memory_search_handler(self, params: dict[str, Any]) -> MemorySearchResult:
        assert self._sessions is not None
        cmd = MemorySearchCommand.model_validate(params)
        memories = self._sessions.search_memory(
            cmd.query,
            session_id=cmd.session_id,
            workspace_root=cmd.workspace_root,
            limit=cmd.limit,
        )
        return MemorySearchResult(memories=memories)

    # 列出 active 长期记忆和 Phase 1 原始项
    async def _memory_list_handler(self, params: dict[str, Any]) -> MemoryListResult:
        assert self._sessions is not None
        cmd = MemoryListCommand.model_validate(params)
        store = self._memory_store
        memories = (
            store.list_memories(
                scope=cmd.scope,
                session_id=cmd.session_id,
                workspace_root=cmd.workspace_root,
                include_deleted=cmd.include_deleted,
                limit=cmd.limit,
            )
            if store is not None
            else []
        )
        return MemoryListResult(memories=memories)

    # 删除长期记忆，实际写入 tombstone
    async def _memory_delete_handler(self, params: dict[str, Any]) -> MemoryDeleteResult:
        assert self._sessions is not None
        cmd = MemoryDeleteCommand.model_validate(params)
        deleted = await self._sessions.delete_memory(cmd.memory_id, cmd.reason)
        return MemoryDeleteResult(deleted=deleted)

    # 接收客户端权限审批响应，resolve 对应挂起的 Future
    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        cmd = PermissionRespondCommand.model_validate(params)
        logger.info(
            "permission.respond received tool_use_id=%s decision=%s",
            cmd.tool_use_id, cmd.decision,
        )
        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            return PermissionRespondResult()
        try:
            self._permission_manager.respond(cmd.tool_use_id, cmd.decision)
        except ValueError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return PermissionRespondResult()

    # 手动压缩 session thread，将摘要持久化写入 thread.jsonl
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result  # type: ignore[no-any-return]

    # 列出会话的文件修改记录，包括中断提交的核对状态
    async def _file_history_list_handler(self, params: dict[str, Any]) -> FileHistoryListResult:
        assert self._sessions is not None
        cmd = FileHistoryListCommand.model_validate(params)
        result = await self._sessions.file_history(cmd.session_id, "list")
        return FileHistoryListResult.model_validate(result)

    # 返回历史中一次文件修改的文字差异
    async def _file_history_diff_handler(self, params: dict[str, Any]) -> FileHistoryDiffResult:
        assert self._sessions is not None
        cmd = FileHistoryDiffCommand.model_validate(params)
        result = await self._sessions.file_history(
            cmd.session_id, "diff", cmd.change_id, from_change_id=cmd.from_change_id,
        )
        return FileHistoryDiffResult.model_validate(result)

    # 校验当前文件版本后恢复一次修改，支持只读预览
    async def _file_history_restore_handler(
        self, params: dict[str, Any]
    ) -> FileHistoryRestoreResult:
        assert self._sessions is not None
        cmd = FileHistoryRestoreCommand.model_validate(params)
        result = await self._sessions.file_history(
            cmd.session_id, "restore", cmd.change_id, cmd.dry_run
        )
        return FileHistoryRestoreResult.model_validate(result)

    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        await self._sessions.close(cmd.session_id)
        return SessionCloseResult(status="closed")

    # 注册客户端事件订阅，可选先回放 events.jsonl 历史再接收实时流
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()

        replayed_count = 0
        if cmd.replay_from_run is not None:
            replayed_count = await self._replay_events(
                cmd.replay_from_run, writer, cmd.topics
            )

        assert self._broadcaster is not None
        sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

    # 从 events.jsonl 向 writer 回放匹配 topic 的历史事件，返回已回放条数
    async def _replay_events(
        self,
        run_id: str,
        writer: asyncio.StreamWriter,
        topics: list[str],
    ) -> int:
        # 新版日志位于 session 根目录；此路径仅用于读取旧版本的全局 runs 数据。
        path = Path("runs") / run_id / "events.jsonl"
        unified_session_log = False
        if not path.exists():
            assert self._sessions_root is not None
            # 兼容旧版 session/runs/<run_id>/events.jsonl 布局。
            for candidate in self._sessions_root.glob(
                f"*/*/*/*/runs/{run_id}/events.jsonl"
            ):
                path = candidate
                break
        if not path.exists():
            assert self._sessions_root is not None
            # 新版 session 将全部 run 汇总到 session/events.jsonl。
            for candidate in self._sessions_root.glob("*/*/*/*/events.jsonl"):
                if self._event_log_contains_run(candidate, run_id):
                    path = candidate
                    unified_session_log = True
                    break
        if not path.exists():
            return 0

        count = 0
        related_run_ids = {run_id}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_run_id = event.get("run_id")
            parent_run_id = event.get("parent_run_id")
            if unified_session_log:
                if parent_run_id in related_run_ids and isinstance(event_run_id, str):
                    related_run_ids.add(event_run_id)
                if event_run_id not in related_run_ids:
                    continue
            event_type: str = event.get("type", "")
            if not any(fnmatch.fnmatch(event_type, p) for p in topics):
                continue
            notification = JsonRpcNotification(method="event.push", params=event)
            writer.write(notification.model_dump_json().encode() + b"\n")
            count += 1

        if count:
            await writer.drain()
        return count

    # 判断统一 session 事件日志中是否包含指定 run，忽略损坏的 JSONL 行
    def _event_log_contains_run(self, path: Path, run_id: str) -> bool:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("run_id") == run_id:
                return True
        return False

    # 启动常驻 core 进程：加载配置、初始化日志、启动 trace 和 TCP 服务，并等待退出信号
    async def run(self) -> None:
        try:
            await self._serve()
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        logger.info("shutting down pid=%d", os.getpid())
        if self._lifecycle is not None:
            self._lifecycle.close()
        if self._server is not None:
            await self._server.stop()
        run_tasks = [running.task for running in self._running_runs.values()]
        for run_task in run_tasks:
            run_task.cancel()
        if self._running_runs:
            await asyncio.gather(*run_tasks, return_exceptions=True)
        if self._sessions is not None:
            try:
                await asyncio.wait_for(self._sessions.wait_for_memory_tasks(), timeout=10)
            except TimeoutError:
                logger.warning("memory cleanup timed out during shutdown")
        if self._task_manager is not None:
            await self._task_manager.cancel_all()
        if self._mcp_manager is not None:
            await self._mcp_manager.stop_all()
        if self._trace is not None:
            await self._trace.stop()

    async def _serve(self) -> None:
        self._start_time = time.monotonic()
        self._config = get_config()
        setup_logging(self._config)

        if self._config.trace.enabled:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(trace_path)
            await self._trace.start()
            self._bus.subscribe(self._trace_event_handler)

        policy_file = Path("~/.agentlite/policy.toml").expanduser()
        self._permission_manager = PermissionManager(
            policy_file=policy_file,
            timeout_s=self._config.permission.timeout_s,
        )
        logger.info(
            "permission manager: timeout_s=%.1f  persistent=%d entries",
            self._config.permission.timeout_s,
            len(load_policy_file(policy_file)),
        )

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)
        self._sessions_root = Path(self._config.session.dir).expanduser().resolve()
        store = SessionStore(self._sessions_root)
        await asyncio.to_thread(recover_file_histories, self._sessions_root)
        self._task_manager = SubagentTaskManager(store.tasks_dir)
        logger.info("sessions: root=%s", self._sessions_root)
        self._memory_store = MemoryStore(Path(self._config.memory.dir).expanduser())
        logger.info("memory: db=%s", self._memory_store.path)
        assert self._config is not None
        llm_config = self._config.llm
        compact_provider = DeferredProvider(
            lambda: create_llm_provider(resolve_model(llm_config, None, None))
        )

        self._mcp_manager = McpServerManager()
        await self._mcp_manager.start_all(self._config.mcp.servers)

        self._sessions = SessionManager(
            store,
            runner_factory=lambda: AgentRunner(
                self._config,  # type: ignore[arg-type]
                bus=self._bus,
                trace=self._trace,
                permission_manager=self._permission_manager,
                mcp_manager=self._mcp_manager,
                task_manager=self._task_manager,
                input_manager=self._input_manager,
            ),
            bus=self._bus,
            provider=compact_provider,
            provider_factory=lambda session: create_llm_provider(
                with_reasoning(
                    resolve_model(llm_config, session.model_id, session.workspace_root),
                    session.reasoning_effort,
                )
            ),
            memory_store=self._memory_store,
            memory_use_enabled=self._config.memory.use_enabled,
            memory_generate_enabled=self._config.memory.generate_enabled,
            memory_min_rollout_idle_hours=self._config.memory.min_rollout_idle_hours,
            memory_max_rollout_age_days=self._config.memory.max_rollout_age_days,
            task_manager=self._task_manager,
            default_permission_mode=self._config.permission.default_mode,
        )

        shutdown = asyncio.Event()
        self._shutdown = shutdown
        self._lifecycle = FrontendLifecycle(
            shutdown.set, managed=os.environ.get("AGENTLITE_FRONTEND_MANAGED") == "1"
        )
        server = SocketServer(
            self._config.host,
            self._config.port,
            self._broadcaster,
            trace=self._trace,
            on_disconnect=self._lifecycle.unregister,
        )
        # 将 RPC 方法名注册到对应的异步处理函数
        self._server = server
        server.register("core.ping", self._ping_handler)
        server.register("core.shutdown", self._shutdown_handler)
        server.register("core.keep_alive", self._keep_alive_handler)
        server.register("frontend.register", self._frontend_register)
        server.register("frontend.heartbeat", self._frontend_heartbeat)
        server.register("frontend.unregister", self._frontend_unregister)
        server.register("agent.run", self._agent_run_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.list", self._session_list_handler)
        server.register("session.rename", self._session_rename_handler)
        server.register("session.set_model", self._session_model_handler)
        server.register("session.reasoning", self._session_reasoning_handler)
        server.register("session.permission_mode", self._session_permission_mode_handler)
        server.register("session.collaboration", self._session_collaboration_handler)
        server.register("user_input.respond", self._user_input_handler)
        server.register("model.list", self._model_list_handler)
        server.register("mcp.list", self._mcp_list_handler)
        server.register("skill.list", self._skill_list_handler)
        server.register("mcp.manage", self._mcp_manage_handler)
        server.register("session.resume", self._session_resume_handler)
        server.register("session.set_workspace", self._session_set_workspace_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.cancel", self._session_cancel_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.set_memory", self._session_set_memory_handler)
        server.register("session.set_stats", self._session_set_stats_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("session.compact", self._session_compact_handler)
        server.register("file_history.list", self._file_history_list_handler)
        server.register("file_history.diff", self._file_history_diff_handler)
        server.register("file_history.restore", self._file_history_restore_handler)
        server.register("memory.search", self._memory_search_handler)
        server.register("memory.list", self._memory_list_handler)
        server.register("memory.delete", self._memory_delete_handler)

        addr = await server.start()  # 启动监听；新连接由 SocketServer 的回调处理
        self._lifecycle.start()
        logger.info("agentlite-core %s listening addr=%s", agent_lite.__version__, addr)
        logger.info("config: %s", self._config)

        loop = asyncio.get_running_loop()  # 获取 asyncio.run 已启动的事件循环
        previous_signal_handlers = {
            sig: signal.signal(
                sig,
                lambda _signum, _frame: loop.call_soon_threadsafe(shutdown.set),
            )
            for sig in (signal.SIGINT, signal.SIGTERM)
        }

        try:
            # 挂起主协程，直到信号或 core.shutdown 触发事件。
            await shutdown.wait()
        finally:
            for sig, previous in previous_signal_handlers.items():
                signal.signal(sig, previous)




# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    asyncio.run(CoreApp().run())
