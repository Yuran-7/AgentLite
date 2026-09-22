from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from agent_lite.core.context import ExecutionContext


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class SubagentTaskRecord:
    task_id: str
    session_id: str
    owner_run_id: str
    agent_type: str
    description: str
    output_file: str
    status: str = "running"
    result: str = ""
    error: str = ""
    created_at: str = ""
    finished_at: str | None = None
    notification_enqueued: bool = False
    notification_delivered: bool = False


NotificationHandler = Callable[[str, str, str], Awaitable[None]]
NotificationQueuedHandler = Callable[[str, str], Awaitable[None]]
NotificationDeliveredHandler = Callable[[str, str, str], Awaitable[None]]


class SubagentTaskManager:
    """Own background subagent tasks and route their completion notifications."""

    def __init__(self, task_dir: Callable[[str], Path]) -> None:
        self._task_dir = task_dir
        self._records: dict[str, SubagentTaskRecord] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._contexts: dict[str, ExecutionContext] = {}
        self._active_runs: set[str] = set()
        self._run_notifications: dict[str, list[tuple[str, str, str]]] = {}
        self._routed_notifications: set[str] = set()
        self._handler: NotificationHandler | None = None
        self._queued_handler: NotificationQueuedHandler | None = None
        self._delivered_handler: NotificationDeliveredHandler | None = None
        self._lock = asyncio.Lock()

    def set_notification_handler(self, handler: NotificationHandler) -> None:
        self._handler = handler

    def set_notification_event_handlers(
        self,
        queued: NotificationQueuedHandler,
        delivered: NotificationDeliveredHandler,
    ) -> None:
        self._queued_handler = queued
        self._delivered_handler = delivered

    def output_path(self, session_id: str, task_id: str) -> Path:
        return self._task_dir(session_id) / f"{task_id}.txt"

    def register(
        self,
        *,
        task_id: str,
        task: asyncio.Task[None],
        context: ExecutionContext,
        session_id: str,
        owner_run_id: str,
        agent_type: str,
        description: str,
    ) -> SubagentTaskRecord:
        output_path = self.output_path(session_id, task_id)
        record = SubagentTaskRecord(
            task_id=task_id,
            session_id=session_id,
            owner_run_id=owner_run_id,
            agent_type=agent_type,
            description=description,
            output_file=str(output_path.resolve()),
            created_at=_now(),
        )
        self._records[task_id] = record
        self._tasks[task_id] = task
        self._contexts[task_id] = context
        self._write_output(record)
        self._write_record(record)
        return record

    def get(self, task_id: str) -> SubagentTaskRecord | None:
        return self._records.get(task_id)

    async def activate_run(self, run_id: str) -> None:
        async with self._lock:
            self._active_runs.add(run_id)

    async def drain_run_notifications(self, run_id: str) -> list[tuple[str, str]]:
        async with self._lock:
            queued = self._run_notifications.pop(run_id, [])
            for task_id, _session_id, _message in queued:
                record = self._records.get(task_id)
                if record is not None:
                    record.notification_delivered = True
                    self._write_record(record)
        if self._delivered_handler is not None:
            for task_id, session_id, _message in queued:
                await self._delivered_handler(session_id, task_id, run_id)
        return [(task_id, message) for task_id, _session_id, message in queued]

    async def deactivate_run(self, run_id: str) -> None:
        async with self._lock:
            self._active_runs.discard(run_id)
            queued = self._run_notifications.pop(run_id, [])
        for task_id, session_id, message in queued:
            await self._deliver_to_session(task_id, session_id, message)

    async def finish(
        self,
        task_id: str,
        *,
        status: str,
        result: str = "",
        error: str = "",
    ) -> None:
        async with self._lock:
            record = self._records.get(task_id)
            if record is None or record.notification_enqueued:
                return
            record.status = status
            record.result = result
            record.error = error
            record.finished_at = _now()
            record.notification_enqueued = True
            self._write_output(record)
            message = self._notification(record)
            self._write_record(record)
            if record.owner_run_id in self._active_runs:
                self._run_notifications.setdefault(record.owner_run_id, []).append(
                    (task_id, record.session_id, message)
                )
                owner_active = True
            else:
                owner_active = False
        if self._queued_handler is not None:
            await self._queued_handler(record.session_id, task_id)
        if not owner_active:
            await self._deliver_to_session(task_id, record.session_id, message)

    async def _deliver_to_session(self, task_id: str, session_id: str, message: str) -> None:
        if self._handler is None:
            return
        async with self._lock:
            record = self._records.get(task_id)
            if (
                record is None
                or record.notification_delivered
                or task_id in self._routed_notifications
            ):
                return
            self._routed_notifications.add(task_id)
        try:
            await self._handler(session_id, task_id, message)
        except Exception:
            async with self._lock:
                self._routed_notifications.discard(task_id)
            raise

    async def release_notification(self, task_id: str) -> None:
        """Allow an undelivered notification to be routed again after a queue drops it."""
        async with self._lock:
            self._routed_notifications.discard(task_id)

    async def mark_notification_delivered(self, task_id: str, run_id: str) -> None:
        """Persist delivery after a notification reaches an agent context."""
        delivered: tuple[str, str] | None = None
        async with self._lock:
            record = self._records.get(task_id)
            if record is not None and not record.notification_delivered:
                record.notification_delivered = True
                self._routed_notifications.discard(task_id)
                self._write_record(record)
                delivered = (record.session_id, record.task_id)
        if delivered is not None and self._delivered_handler is not None:
            await self._delivered_handler(delivered[0], delivered[1], run_id)

    async def recover_session(self, session_id: str) -> None:
        directory = self._task_dir(session_id)
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                record = SubagentTaskRecord(**data)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            live_task = self._tasks.get(record.task_id)
            if live_task is not None and not live_task.done():
                continue
            self._records[record.task_id] = record
            if record.status == "running":
                record.status = "interrupted"
                record.error = "Core process stopped before the subagent completed."
                record.finished_at = _now()
                record.notification_enqueued = True
                record.notification_delivered = False
                self._write_output(record)
                self._write_record(record)
            if record.notification_enqueued and not record.notification_delivered:
                if self._queued_handler is not None:
                    await self._queued_handler(record.session_id, record.task_id)
                await self._deliver_to_session(
                    record.task_id, record.session_id, self._notification(record)
                )

    async def cancel_session(self, session_id: str) -> None:
        tasks = [
            self._tasks[task_id]
            for task_id, record in self._records.items()
            if record.session_id == session_id
            and task_id in self._tasks
            and not self._tasks[task_id].done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel_all(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _record_path(self, record: SubagentTaskRecord) -> Path:
        return self._task_dir(record.session_id) / f"{record.task_id}.json"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    def _write_record(self, record: SubagentTaskRecord) -> None:
        self._atomic_write(
            self._record_path(record),
            json.dumps(asdict(record), ensure_ascii=False, indent=2) + "\n",
        )

    def _write_output(self, record: SubagentTaskRecord) -> None:
        lines = [
            f"task_id: {record.task_id}",
            f"agent_type: {record.agent_type}",
            f"description: {record.description}",
            f"status: {record.status}",
            f"output_file: {record.output_file}",
            f"created_at: {record.created_at}",
        ]
        if record.finished_at is not None:
            lines.append(f"finished_at: {record.finished_at}")
        if record.result:
            lines.extend(["", "result:", record.result])
        if record.error:
            lines.extend(["", "error:", record.error])
        self._atomic_write(Path(record.output_file), "\n".join(lines) + "\n")

    @staticmethod
    def _notification(record: SubagentTaskRecord) -> str:
        if record.status == "completed":
            summary = f'Agent "{record.description}" completed'
        elif record.status == "cancelled":
            summary = f'Agent "{record.description}" was cancelled'
        elif record.status == "interrupted":
            summary = f'Agent "{record.description}" was interrupted'
        else:
            summary = f'Agent "{record.description}" failed'
        result = record.result or record.error
        return (
            "<task-notification>\n"
            f"  <task-id>{escape(record.task_id)}</task-id>\n"
            f"  <output-file>{escape(record.output_file)}</output-file>\n"
            f"  <agent-type>{escape(record.agent_type)}</agent-type>\n"
            f"  <status>{escape(record.status)}</status>\n"
            f"  <summary>{escape(summary)}</summary>\n"
            f"  <result>{escape(result)}</result>\n"
            "</task-notification>"
        )
