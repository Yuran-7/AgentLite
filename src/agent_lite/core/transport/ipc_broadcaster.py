from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel

from agent_lite.core.bus.envelope import JsonRpcNotification
from agent_lite.core.trace.record import TraceRecord
from agent_lite.core.trace.writer import TraceWriter

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class _Subscription:
    sub_id: str
    writer: asyncio.StreamWriter
    topics: list[str]
    scope: str


class IpcEventBroadcaster:
    def __init__(self, trace: TraceWriter | None = None) -> None:
        self._subscriptions: list[_Subscription] = []
        self._run_sessions: dict[str, str] = {}
        self._trace = trace

    # 注册一个客户端订阅，返回 subscription_id
    def subscribe(
        self,
        writer: asyncio.StreamWriter,
        topics: list[str],
        scope: str = "global",
    ) -> str:
        sub_id = f"sub-{uuid.uuid4().hex[:8]}"
        sub = _Subscription(sub_id=sub_id, writer=writer, topics=topics, scope=scope)
        self.unsubscribe(writer)
        self._subscriptions.append(sub)
        return sub_id

    # 移除指定 writer 的所有订阅
    def unsubscribe(self, writer: asyncio.StreamWriter) -> None:
        self._subscriptions = [s for s in self._subscriptions if s.writer is not writer]

    # 将事件推送到所有匹配的订阅客户端，写入失败时延迟清理死连接
    async def handle(self, event: BaseModel) -> None:
        event_dict = event.model_dump()
        notification = JsonRpcNotification(method="event.push", params=event_dict)
        frame = notification.model_dump_json().encode() + b"\n"
        event_type: str = event_dict.get("type", "")
        run_id: str | None = event_dict.get("run_id")
        session_id: str | None = event_dict.get("session_id")

        if run_id and session_id:
            self._run_sessions[run_id] = session_id
        elif event_type == "subagent.started" and run_id:
            parent_run_id: str | None = event_dict.get("parent_run_id")
            if parent_run_id:
                session_id = self._run_sessions.get(parent_run_id)
                if session_id:
                    self._run_sessions[run_id] = session_id
        elif run_id:
            session_id = self._run_sessions.get(run_id)

        dead: list[asyncio.StreamWriter] = []

        for sub in list(self._subscriptions):
            if not self._matches_topic(event_type, sub.topics):
                continue
            if not self._matches_scope(run_id, session_id, sub.scope):
                continue
            try:
                sub.writer.write(frame)
                await sub.writer.drain()
                if self._trace is not None:
                    client_id = str(sub.writer.get_extra_info("peername", "<unknown>"))
                    self._trace.emit(
                        TraceRecord(
                            ts=_now(),
                            direction="CORE→CLIENT",
                            layer="ipc",
                            kind="push",
                            run_id=run_id,
                            client_id=client_id,
                            data={"sub_id": sub.sub_id, "event_type": event_type},
                        )
                    )
            except (ConnectionResetError, BrokenPipeError, OSError):
                logger.debug("dead connection for sub %s, scheduling cleanup", sub.sub_id)
                dead.append(sub.writer)

        for writer in dead:
            self.unsubscribe(writer)

        if event_type in {"run.finished", "subagent.finished"} and run_id:
            self._run_sessions.pop(run_id, None)

    # 检查事件类型是否匹配订阅的 topic 列表（支持 fnmatch glob 模式）
    @staticmethod
    def _matches_topic(event_type: str, topics: list[str]) -> bool:
        return any(fnmatch.fnmatch(event_type, pattern) for pattern in topics)

    # 检查事件是否匹配订阅的 global、run 或 session scope
    @staticmethod
    def _matches_scope(
        run_id: str | None,
        session_id: str | None,
        scope: str,
    ) -> bool:
        if scope == "global":
            return True
        if scope.startswith("run:"):
            return run_id == scope[4:]
        if scope.startswith("session:"):
            return session_id == scope[8:]
        return False
