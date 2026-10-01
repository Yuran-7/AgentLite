from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_lite.core.bus.envelope import INVALID_PARAMS, HandlerError
from agent_lite.core.bus.events import (
    MemoryDeletedEvent,
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionMessageReceivedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SessionWorkspaceSetEvent,
    SkillInvokedEvent,
    TaskNotificationDeliveredEvent,
    TaskNotificationQueuedEvent,
)
from agent_lite.core.events.bus import EventBus
from agent_lite.core.memory.pipeline import MemoryPipeline, sanitize_transcript, transcript_hash
from agent_lite.core.memory.store import MemoryStore
from agent_lite.core.runs import new_run_id
from agent_lite.core.session.ids import new_session_id
from agent_lite.core.session.model import Session, SessionMode
from agent_lite.core.session.store import SessionStore
from agent_lite.core.skills.loader import SkillLoader
from agent_lite.core.subagent.registry import SubagentTaskManager

if TYPE_CHECKING:
    from agent_lite.core.llm.base import LLMProvider
    from agent_lite.core.runner import AgentRunner

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012
SESSION_WORKSPACE_ALREADY_SET = -32013

log = logging.getLogger(__name__)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 将可选工作区规范化为绝对目录路径，无工作区时返回 None
def _normalize_workspace_root(workspace_root: str | None) -> str | None:
    if workspace_root is None or not workspace_root.strip():
        return None
    candidate = Path(workspace_root).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HandlerError(
            INVALID_PARAMS,
            "workspace_root does not exist or cannot be resolved",
            {"workspace_root": workspace_root},
        ) from exc
    if not resolved.is_dir():
        raise HandlerError(
            INVALID_PARAMS,
            "workspace_root must be a directory",
            {"workspace_root": workspace_root},
        )
    return str(resolved)


class SessionManager:
    # 初始化会话管理器，接入文件存储、runner 工厂、事件总线和可选的 LLM provider（用于手动压缩）
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
        memory_store: MemoryStore | None = None,
        memory_use_enabled: bool = True,
        memory_generate_enabled: bool = False,
        memory_min_rollout_idle_hours: int = 6,
        memory_max_rollout_age_days: int = 10,
        task_manager: SubagentTaskManager | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._memory_store = memory_store
        self._memory_use_enabled = memory_use_enabled
        self._memory_generate_enabled = memory_generate_enabled
        self._memory_min_rollout_idle = timedelta(
            hours=max(1, memory_min_rollout_idle_hours)
        )
        self._memory_max_rollout_age = timedelta(days=max(1, memory_max_rollout_age_days))
        self._memory_pipeline = (
            MemoryPipeline(memory_store, provider)
            if memory_store is not None and provider is not None
            else None
        )
        self._memory_tasks: set[asyncio.Task[None]] = set()
        self._memory_scan_lock = asyncio.Lock()
        self._memory_scan_started_sessions: set[str] = set()
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._skill_loader = SkillLoader()
        self._task_manager = task_manager
        self._pending_task_notifications: dict[str, list[tuple[str, str]]] = {}
        self._notification_pumps: dict[str, asyncio.Task[None]] = {}
        if self._task_manager is not None:
            self._task_manager.set_notification_handler(self._on_task_notification)
            self._task_manager.set_notification_event_handlers(
                self._on_task_notification_queued,
                self._on_task_notification_delivered,
            )

    async def _on_task_notification_queued(
        self, session_id: str, task_id: str
    ) -> None:
        await self._bus.publish(
            TaskNotificationQueuedEvent(
                session_id=session_id, task_id=task_id, ts=_now()
            )
        )

    async def _on_task_notification_delivered(
        self, session_id: str, task_id: str, run_id: str
    ) -> None:
        await self._bus.publish(
            TaskNotificationDeliveredEvent(
                session_id=session_id,
                task_id=task_id,
                run_id=run_id,
                ts=_now(),
            )
        )

    async def _on_task_notification(
        self, session_id: str, task_id: str, message: str
    ) -> None:
        self._pending_task_notifications.setdefault(session_id, []).append(
            (task_id, message)
        )
        pump = self._notification_pumps.get(session_id)
        if pump is None or pump.done():
            pump = asyncio.create_task(
                self._drain_task_notifications(session_id),
                name=f"task-notifications-{session_id}",
            )
            self._notification_pumps[session_id] = pump

    async def _drain_task_notifications(self, sid: str) -> None:
        await asyncio.sleep(0.05)
        session = self._sessions.get(sid)
        lock = self._locks.get(sid)
        if session is None or lock is None:
            dropped = self._pending_task_notifications.pop(sid, [])
            if self._task_manager is not None:
                for task_id, _message in dropped:
                    await self._task_manager.release_notification(task_id)
            return

        async with lock:
            batch = self._pending_task_notifications.pop(sid, [])
            if not batch:
                return
            if session.status == "closed" and session.mode != "one_shot":
                if self._task_manager is not None:
                    for task_id, _message in batch:
                        await self._task_manager.release_notification(task_id)
                return

            task_ids = [item[0] for item in batch]
            content = "\n\n".join(item[1] for item in batch)
            run_id = new_run_id()
            self._store.append_message(
                sid,
                "user",
                content,
                run_id=run_id,
                kind="task_notification",
                notification_id=",".join(task_ids),
            )
            if self._task_manager is not None:
                for task_id in task_ids:
                    await self._task_manager.mark_notification_delivered(task_id, run_id)
            session.status = "active"
            session.run_ids.append(run_id)
            self._persist_started_session(session)
            try:
                await self._runner_factory().run_and_capture(
                    content,
                    run_id=run_id,
                    session=session,
                    store=self._store,
                )
            finally:
                session.updated_at = _now()
                session.last_chat_at = session.updated_at
                session.status = (
                    "closed" if session.mode == "one_shot" else "waiting_for_input"
                )
                self._store.write_meta(session)
                if session.mode == "one_shot":
                    await self._bus.publish(
                        SessionClosedEvent(session_id=sid, ts=session.updated_at)
                    )
                else:
                    await self._bus.publish(
                        SessionWaitingForInputEvent(
                            session_id=sid,
                            last_run_id=run_id,
                            ts=session.updated_at,
                        )
                    )

        if self._pending_task_notifications.get(sid):
            next_pump = asyncio.create_task(
                self._drain_task_notifications(sid),
                name=f"task-notifications-{sid}",
            )
            self._notification_pumps[sid] = next_pump

    # 创建新 session；首次发送消息时才创建目录并写入 meta.json
    async def create(
        self,
        mode: SessionMode,
        title: str = "",
        workspace_root: str | None = None,
    ) -> Session:
        sid = new_session_id()
        ts = _now()
        normalized_workspace = _normalize_workspace_root(workspace_root)
        session = Session(
            id=sid,
            mode=mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            workspace_root=normalized_workspace,
            run_ids=[],
            memory_generate_enabled=self._memory_generate_enabled,
            memory_use_enabled=self._memory_use_enabled,
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        await self._bus.publish(
            SessionCreatedEvent(
                session_id=sid,
                mode=mode,
                workspace_root=normalized_workspace,
                ts=ts,
            )
        )
        return session

    # 在后台安排一次历史 Session 扫描。
    def _schedule_memory_scan(self, *, exclude_session_id: str) -> None:
        if self._memory_pipeline is None:
            return
        task = asyncio.create_task(
            self._scan_historical_sessions(exclude_session_id=exclude_session_id),
            name="memory-phase1-scan",
        )
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_tasks.discard)

    # 列出可恢复的 chat session，可选限制为指定工作区
    def list_sessions(self, workspace_root: str | None = None) -> list[Session]:
        sessions: list[Session] = []
        for session in self._store.list_sessions(workspace_root):
            if session.last_chat_at is None:
                session.last_chat_at = self._store.last_message_at(session.id)
                if session.last_chat_at is not None:
                    self._store.write_meta(session)
            if session.last_chat_at is not None:
                session.updated_at = session.last_chat_at
            sessions.append(session)
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    # 从磁盘恢复 chat session，并可在用户确认后切换到新的工作区
    async def resume(self, sid: str, workspace_root: str | None = None) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            try:
                session = self._store.read_meta(sid)
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise HandlerError(SESSION_NOT_FOUND, "session not found") from exc
            self._sessions[sid] = session
            self._locks[sid] = asyncio.Lock()
        if session.mode != "chat":
            raise HandlerError(INVALID_PARAMS, "only chat sessions can be resumed")

        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            if workspace_root is not None:
                session.workspace_root = _normalize_workspace_root(workspace_root)
            session.status = (
                "waiting_for_input" if self._store.read_messages(sid) else "active"
            )
            self._store.write_meta(session)
            await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))
            if self._task_manager is not None:
                asyncio.create_task(
                    self._task_manager.recover_session(sid),
                    name=f"recover-subagents-{sid}",
                )
            return session

    # 为未绑定工作区的 session 设置工作区；未开始对话时只更新内存
    async def set_workspace(self, sid: str, workspace_root: str) -> str:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            normalized_workspace = _normalize_workspace_root(workspace_root)
            if normalized_workspace is None:
                raise HandlerError(INVALID_PARAMS, "workspace_root must not be empty")
            if session.workspace_root == normalized_workspace:
                return normalized_workspace
            if session.workspace_root is not None:
                raise HandlerError(
                    SESSION_WORKSPACE_ALREADY_SET,
                    "session workspace is already set; create a new session to switch it",
                    {
                        "workspace_root": session.workspace_root,
                        "requested_workspace_root": normalized_workspace,
                    },
                )

            session.workspace_root = normalized_workspace
            self._persist_started_session(session)
            await self._bus.publish(
                SessionWorkspaceSetEvent(
                    session_id=sid,
                    workspace_root=normalized_workspace,
                    ts=_now(),
                )
            )
            return normalized_workspace

    # 处理用户消息，追加 thread 并启动一次 agent run
    async def send_message(self, sid: str, content: str, *, run_id: str | None = None) -> str:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            memory_context = ""
            if self._memory_store is not None and session.memory_use_enabled:
                memory_context = self._memory_store.format_memory_summary()

            if session.status == "waiting_for_input":
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

            self._store.append_message(sid, "user", content)
            await self._bus.publish(
                SessionMessageReceivedEvent(session_id=sid, content=content, ts=_now())
            )
            if sid not in self._memory_scan_started_sessions:
                self._memory_scan_started_sessions.add(sid)
                self._schedule_memory_scan(exclude_session_id=sid)

            if not session.title:
                session.title = content[:40]

            run_id = run_id or new_run_id()
            session.run_ids.append(run_id)
            self._persist_started_session(session)

            # Skill 解析：检测 "/" 前缀，展开为系统提示覆盖和工具白名单
            goal = content
            system_prompt_override: str | None = None
            tool_whitelist: list[str] | None = None
            if content.startswith("/"):
                parts = content[1:].split(None, 1)
                skill_name = parts[0]
                arguments = parts[1] if len(parts) > 1 else ""
                skill = self._skill_loader.resolve(skill_name)
                if skill is not None:
                    goal = self._skill_loader.render_prompt(skill, arguments)
                    system_prompt_override = skill.system_prompt_template
                    tool_whitelist = skill.allowed_tools or None
                    await self._bus.publish(
                        SkillInvokedEvent(
                            skill_name=skill_name,
                            arguments=arguments,
                            run_id=run_id,
                            ts=_now(),
                        )
                    )

            runner = self._runner_factory()
            runner_kwargs: dict[str, Any] = {
                "run_id": run_id,
                "session": session,
                "store": self._store,
                "system_prompt_override": system_prompt_override,
                "tool_whitelist": tool_whitelist,
            }
            if memory_context:
                runner_kwargs["memory_context"] = memory_context
            try:
                await runner.run_and_capture(goal, **runner_kwargs)

            finally:
                # 取消和异常也必须恢复 Session 状态并释放 async with lock。
                session.updated_at = _now()
                session.last_chat_at = session.updated_at
                if session.mode == "one_shot":
                    session.status = "closed"
                    await self._bus.publish(
                        SessionClosedEvent(session_id=sid, ts=session.updated_at)
                    )
                else:
                    session.status = "waiting_for_input"
                    await self._bus.publish(
                        SessionWaitingForInputEvent(
                            session_id=sid,
                            last_run_id=run_id,
                            ts=session.updated_at,
                        )
                    )
                self._store.write_meta(session)
            return run_id

    async def _run_memory_pipeline(
        self,
        session: Session,
        run_id: str | None,
        transcript: list[dict[str, Any]],
    ) -> None:
        assert self._memory_pipeline is not None
        try:
            await self._memory_pipeline.process_session(
                session_id=session.id,
                transcript=transcript,
            )
            session.last_memory_extracted_run_id = run_id
            session.memory_generation_status = "succeeded"
            self._persist_started_session(session)
        except asyncio.CancelledError:
            session.memory_generation_status = "failed"
            self._persist_started_session(session)
            raise
        except Exception:
            session.memory_generation_status = "failed"
            self._persist_started_session(session)
            log.exception("memory pipeline task failed session_id=%s run_id=%s", session.id, run_id)

    # 扫描已空闲的历史 Session，并为有限数量的候选生成 rollout summary。
    async def _scan_historical_sessions(
        self, *, exclude_session_id: str | None = None
    ) -> None:
        if self._memory_pipeline is None or self._memory_scan_lock.locked():
            return
        async with self._memory_scan_lock:
            now = datetime.now(UTC)
            candidates: list[tuple[Session, list[dict[str, Any]]]] = []
            for session in self.list_sessions():
                if session.id == exclude_session_id or not session.memory_generate_enabled:
                    continue
                try:
                    updated_at = datetime.fromisoformat(session.updated_at)
                except ValueError:
                    continue
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=UTC)
                age = now - updated_at
                if not self._memory_min_rollout_idle <= age <= self._memory_max_rollout_age:
                    continue
                transcript = self._store.read_messages(session.id)
                source_hash = transcript_hash(sanitize_transcript(transcript))
                if self._memory_store is not None and self._memory_store.is_rollout_summary_current(
                    session.id, source_hash
                ):
                    continue
                candidates.append((session, transcript))
                if candidates:
                    break
            for session, transcript in candidates:
                session.memory_generation_status = "running"
                self._store.write_meta(session)
                run_id = session.run_ids[-1] if session.run_ids else None
                await self._run_memory_pipeline(
                    session,
                    run_id,
                    transcript,
                )
            try:
                await self._memory_pipeline.consolidate_if_needed()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("memory Phase 2 consolidation failed")

    async def wait_for_memory_tasks(self) -> None:
        """Wait for currently scheduled background memory tasks (primarily for shutdown/tests)."""

        tasks = list(self._memory_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # 修改当前 session 的记忆生成和检索开关，并持久化到 meta.json
    async def set_memory(
        self,
        sid: str,
        *,
        generate_enabled: bool | None = None,
        use_enabled: bool | None = None,
    ) -> Session:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")
            if generate_enabled is not None:
                session.memory_generate_enabled = generate_enabled
            if use_enabled is not None:
                session.memory_use_enabled = use_enabled
            self._persist_started_session(session)
            return session

    # 保存 TUI 为当前 session 汇总的上下文、耗时和 token 统计，不改变最后聊天时间
    async def set_ui_stats(self, sid: str, stats: dict[str, Any]) -> dict[str, Any]:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            session.ui_stats = dict(stats)
            self._persist_started_session(session)
            return dict(session.ui_stats)

    # 查询当前 session 可见的长期记忆
    def search_memory(
        self,
        query: str,
        *,
        session_id: str | None = None,
        workspace_root: str | None = None,
        limit: int = 5,
    ) -> list[Any]:
        if self._memory_store is None:
            return []
        return self._memory_store.search(
            query,
            session_id=session_id,
            workspace_root=workspace_root,
            limit=limit,
        )

    # 以 tombstone 方式删除长期记忆
    async def delete_memory(self, memory_id: str, reason: str = "user_requested") -> bool:
        if self._memory_store is None:
            return False
        deleted = self._memory_store.delete(memory_id, reason)
        if deleted:
            await self._bus.publish(MemoryDeletedEvent(memory_id=memory_id, ts=_now()))
        return deleted

    # 关闭指定 session；未开始对话时不创建 session 目录
    async def close(self, sid: str) -> None:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            session.status = "closed"
            self._persist_started_session(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=_now()))
        if self._task_manager is not None:
            await self._task_manager.cancel_session(sid)

    # 手动压缩指定 session 的 thread，将摘要持久化写入 thread.jsonl
    async def compact(self, sid: str, focus: str = "") -> Any:
        self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if self._provider is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with lock:
            from agent_lite.core.bus.commands import SessionCompactResult
            from agent_lite.core.compact.compactor import Compactor
            messages = self._store.read_messages(sid)
            session_dir = self._store.session_dir(sid)
            compactor = Compactor(self._bus, session_dir, sid)
            result = await compactor.compact_messages(messages, self._provider, focus=focus)
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")
            self._store.write_compacted(sid, [
                {"role": "user", "content": result.summary_text},
                {"role": "assistant", "content": "Understood, I'll continue from this summary."},
            ])
            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(0, result.original_token_estimate - result.summary_tokens),
            )

    # 读取指定 session 的完整 thread 历史
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        self._get_session(sid)
        return self._store.read_history_messages(sid)

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session

    # 只有已产生对话的 session 才需要落盘，避免为新建但未使用的 TUI 建目录
    def _persist_started_session(self, session: Session) -> None:
        if session.run_ids:
            self._store.write_meta(session)
