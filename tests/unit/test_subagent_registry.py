from __future__ import annotations

import asyncio

from agent_lite.core.context import ExecutionContext
from agent_lite.core.subagent.registry import BackgroundTaskRegistry


# 功能：父 run 收尾时会取消并回收所有未完成的后台 subagent
# 设计：登记两个永久等待的 Task，cancel_all 返回后二者都必须已取消
async def test_cancel_all_cancels_and_joins_background_tasks() -> None:
    registry = BackgroundTaskRegistry()
    tasks: list[asyncio.Task[None]] = []

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    for index in range(2):
        task = asyncio.create_task(wait_forever())
        context = ExecutionContext(run_id=f"child-{index}", goal="work", max_steps=1)
        registry.register(context.run_id, task, context)
        tasks.append(task)

    await asyncio.sleep(0)
    await registry.cancel_all()

    assert all(task.cancelled() for task in tasks)
