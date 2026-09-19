from __future__ import annotations

import asyncio

from agent_lite.core.app import CoreApp


class _BlockingSessions:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleaned = asyncio.Event()

    # 模拟长时间 Agent run，并在取消展开时记录清理已执行
    async def send_message(self, session_id: str, content: str, *, run_id: str) -> str:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaned.set()
        return run_id


# 功能：Core 能按 run_id/session_id 取消业务 Task，而 RPC Task 仍正常返回
# 设计：先用错误 Session 验证不误伤，再取消正确 run 并检查幂等行为
async def test_session_cancel_targets_registered_business_task() -> None:
    app = CoreApp()
    sessions = _BlockingSessions()
    app._sessions = sessions  # type: ignore[assignment]

    send_rpc = asyncio.create_task(
        app._session_send_handler(  # type: ignore[attr-defined]
            {"session_id": "session-1", "content": "work"}
        )
    )
    await sessions.started.wait()
    run_id = next(iter(app._running_runs))  # type: ignore[attr-defined]

    wrong = await app._session_cancel_handler(  # type: ignore[attr-defined]
        {"session_id": "session-other", "run_id": run_id}
    )
    assert not wrong.accepted
    assert not send_rpc.done()

    accepted = await app._session_cancel_handler(  # type: ignore[attr-defined]
        {"session_id": "session-1", "run_id": run_id}
    )
    result = await asyncio.wait_for(send_rpc, timeout=1)

    assert accepted.accepted
    assert result.run_id == run_id
    assert sessions.cleaned.is_set()
    assert app._running_runs == {}  # type: ignore[attr-defined]

    duplicate = await app._session_cancel_handler(  # type: ignore[attr-defined]
        {"session_id": "session-1", "run_id": run_id}
    )
    assert not duplicate.accepted
