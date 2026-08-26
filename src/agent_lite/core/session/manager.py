from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_lite.core.bus.envelope import INVALID_PARAMS, HandlerError
from agent_lite.core.bus.events import (
    MemoryCandidatesCreatedEvent,
    MemoryDeletedEvent,
    MemorySavedEvent,
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionMessageReceivedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SessionWorkspaceSetEvent,
    SkillInvokedEvent,
)
from agent_lite.core.events.bus import EventBus
from agent_lite.core.memory.store import MemoryStore
from agent_lite.core.runs import new_run_id
from agent_lite.core.session.ids import new_session_id
from agent_lite.core.session.model import Session, SessionMode
from agent_lite.core.session.store import SessionStore
from agent_lite.core.skills.loader import SkillLoader

if TYPE_CHECKING:
    from agent_lite.core.llm.base import LLMProvider
    from agent_lite.core.runner import AgentRunner
    from agent_lite.core.tools.builtin.browser_session import BrowserSessionManager

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012
SESSION_WORKSPACE_ALREADY_SET = -32013


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
        browser_manager: BrowserSessionManager | None = None,
        memory_store: MemoryStore | None = None,
        memory_use_enabled: bool = True,
        memory_generate_enabled: bool = False,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._browser_manager = browser_manager
        self._memory_store = memory_store
        self._memory_use_enabled = memory_use_enabled
        self._memory_generate_enabled = memory_generate_enabled
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._skill_loader = SkillLoader()

    # 创建新 session 并写入 meta.json
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
        self._store.write_meta(session)
        await self._bus.publish(
            SessionCreatedEvent(
                session_id=sid,
                mode=mode,
                workspace_root=normalized_workspace,
                ts=ts,
            )
        )
        return session

    # 为未绑定工作区的空闲 session 首次设置工作区并持久化
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
            session.updated_at = _now()
            self._store.write_meta(session)
            await self._bus.publish(
                SessionWorkspaceSetEvent(
                    session_id=sid,
                    workspace_root=normalized_workspace,
                    ts=session.updated_at,
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

            if session.status == "waiting_for_input":
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

            self._store.append_message(sid, "user", content)
            await self._bus.publish(
                SessionMessageReceivedEvent(session_id=sid, content=content, ts=_now())
            )

            if not session.title:
                session.title = content[:40]

            run_id = run_id or new_run_id()
            session.run_ids.append(run_id)
            session.updated_at = _now()
            self._store.write_meta(session)

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
            if self._memory_store is not None and session.memory_use_enabled:
                memory_context = self._memory_store.format_relevant(
                    content,
                    workspace_root=session.workspace_root,
                    session_id=session.id,
                )
                if memory_context:
                    runner_kwargs["memory_context"] = memory_context
            outcome = await runner.run_and_capture(goal, **runner_kwargs)

            if (
                self._memory_store is not None
                and session.memory_generate_enabled
                and outcome.status == "success"
            ):
                candidates = self._memory_store.generate_from_messages(
                    self._store.read_messages(session.id),
                    session_id=session.id,
                    run_id=run_id,
                    workspace_root=session.workspace_root,
                )
                if candidates:
                    await self._bus.publish(
                        MemoryCandidatesCreatedEvent(
                            session_id=session.id,
                            run_id=run_id,
                            count=len(candidates),
                            ts=_now(),
                        )
                    )

            session.updated_at = _now()
            if session.mode == "one_shot":
                session.status = "closed"
                if self._browser_manager is not None:
                    await self._browser_manager.close_session(sid)
                await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))
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
            session.updated_at = _now()
            self._store.write_meta(session)
            return session

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

    # 从 session 历史生成 pending 候选，不会直接写入 active memories
    def generate_memory_candidates(
        self,
        sid: str,
        *,
        run_id: str | None = None,
        content: str | None = None,
    ) -> list[Any]:
        if self._memory_store is None:
            return []
        session = self._get_session(sid)
        messages = self._store.read_messages(sid)
        if content:
            messages = [{"role": "user", "content": content}]
        return self._memory_store.generate_from_messages(
            messages,
            session_id=sid,
            run_id=run_id,
            workspace_root=session.workspace_root,
        )

    # 提交候选记忆，并通过事件通知 TUI
    async def commit_memory_candidate(self, candidate_id: str) -> Any:
        if self._memory_store is None:
            return None
        record = self._memory_store.commit_candidate(candidate_id)
        if record is not None:
            await self._bus.publish(
                MemorySavedEvent(
                    memory_id=record.id,
                    scope=record.scope,
                    key=record.key,
                    ts=_now(),
                )
            )
        return record

    # 以 tombstone 方式删除长期记忆
    async def delete_memory(self, memory_id: str, reason: str = "user_requested") -> bool:
        if self._memory_store is None:
            return False
        deleted = self._memory_store.delete(memory_id, reason)
        if deleted:
            await self._bus.publish(MemoryDeletedEvent(memory_id=memory_id, ts=_now()))
        return deleted

    # 关闭指定 session 并更新 meta.json
    async def close(self, sid: str) -> None:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            session.status = "closed"
            if self._browser_manager is not None:
                await self._browser_manager.close_session(sid)
            session.updated_at = _now()
            self._store.write_meta(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

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
        return self._store.read_messages(sid)

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session
