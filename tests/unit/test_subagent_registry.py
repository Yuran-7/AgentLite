from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent_lite.core.context import ExecutionContext
from agent_lite.core.subagent.registry import SubagentTaskManager


async def test_finish_writes_output_and_notifies_once(tmp_path: Path) -> None:
    manager = SubagentTaskManager(lambda sid: tmp_path / sid / "tasks")
    received: list[tuple[str, str, str]] = []

    async def handler(sid: str, task_id: str, message: str) -> None:
        received.append((sid, task_id, message))

    manager.set_notification_handler(handler)
    sleeper = asyncio.create_task(asyncio.sleep(10))
    context = ExecutionContext(run_id="child", goal="work", max_steps=1)
    record = manager.register(
        task_id="child", task=sleeper, context=context, session_id="session",
        owner_run_id="parent", agent_type="general-purpose", description="do work",
    )
    await manager.finish("child", status="completed", result="done")
    await manager.finish("child", status="completed", result="duplicate")
    assert len(received) == 1
    assert "<result>done</result>" in received[0][2]
    assert Path(record.output_file).read_text(encoding="utf-8").endswith("done\n")
    sleeper.cancel()
    await asyncio.gather(sleeper, return_exceptions=True)


async def test_active_owner_receives_notification_at_safe_boundary(tmp_path: Path) -> None:
    manager = SubagentTaskManager(lambda sid: tmp_path / sid / "tasks")
    task = asyncio.create_task(asyncio.sleep(10))
    manager.register(
        task_id="child", task=task,
        context=ExecutionContext(run_id="child", goal="work", max_steps=1),
        session_id="session", owner_run_id="parent", agent_type="explore",
        description="inspect",
    )
    await manager.activate_run("parent")
    await manager.finish("child", status="completed", result="found")
    messages = await manager.drain_run_notifications("parent")
    assert len(messages) == 1
    assert messages[0][0] == "child"
    assert "found" in messages[0][1]
    await manager.deactivate_run("parent")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_recovery_marks_running_task_interrupted(tmp_path: Path) -> None:
    task_dir = tmp_path / "session" / "tasks"
    task_dir.mkdir(parents=True)
    record = {
        "task_id": "old", "session_id": "session", "owner_run_id": "parent",
        "agent_type": "plan", "description": "old task",
        "output_file": str(task_dir / "old.txt"), "status": "running",
        "result": "", "error": "", "created_at": "before", "finished_at": None,
        "notification_enqueued": False, "notification_delivered": False,
    }
    (task_dir / "old.json").write_text(json.dumps(record), encoding="utf-8")
    manager = SubagentTaskManager(lambda sid: tmp_path / sid / "tasks")
    received: list[str] = []

    async def handler(_sid: str, _task_id: str, message: str) -> None:
        received.append(message)

    manager.set_notification_handler(handler)
    await manager.recover_session("session")
    await manager.recover_session("session")
    assert len(received) == 1
    assert "<status>interrupted</status>" in received[0]


async def test_recovery_does_not_interrupt_live_task(tmp_path: Path) -> None:
    manager = SubagentTaskManager(lambda sid: tmp_path / sid / "tasks")
    task = asyncio.create_task(asyncio.sleep(10))
    manager.register(
        task_id="live",
        task=task,
        context=ExecutionContext(run_id="live", goal="work", max_steps=1),
        session_id="session",
        owner_run_id="parent",
        agent_type="general-purpose",
        description="live task",
    )

    await manager.recover_session("session")

    assert manager.get("live").status == "running"  # type: ignore[union-attr]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
