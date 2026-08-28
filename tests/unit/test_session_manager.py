from __future__ import annotations

from pathlib import Path

import pytest

from agent_lite.core.bus.envelope import INVALID_PARAMS, HandlerError
from agent_lite.core.events.bus import EventBus
from agent_lite.core.memory.store import MemoryStore
from agent_lite.core.runner import RunOutcome
from agent_lite.core.session.manager import (
    SESSION_CLOSED,
    SESSION_NOT_FOUND,
    SESSION_WORKSPACE_ALREADY_SET,
    SessionManager,
)
from agent_lite.core.session.model import Session
from agent_lite.core.session.store import SessionStore


class _Runner:
    # 模拟 AgentRunner，将 run 新消息写入 thread 后返回成功
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        assert run_id is not None
        assert session is not None
        assert store is not None
        store.append_messages(
            session.id,
            [{"role": "assistant", "content": [{"type": "text", "text": f"done {goal}"}]}],
            run_id,
        )
        return RunOutcome(status="success", result="done", reason=None)


# 功能：验证 create 只创建内存 session，首次消息后才写入 meta
# 设计：覆盖 session 目录的延迟持久化边界
async def test_create_session_persists_only_after_first_message(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]

    session = await manager.create("chat", "title")

    assert session.status == "active"
    assert not store.session_dir(session.id).exists()
    await manager.send_message(session.id, "hello")
    assert store.read_meta(session.id).title == "title"
    assert [e.type for e in events] == [  # type: ignore[attr-defined]
        "session.created",
        "session.message_received",
        "session.waiting_for_input",
    ]


# 功能：验证 create 会规范化工作区，首次消息后才持久化
# 设计：覆盖延迟持久化链路
async def test_create_session_persists_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    events: list[object] = []
    bus = EventBus()

    # 收集 session 创建事件以检查其中的规范化工作区
    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]

    session = await manager.create("chat", workspace_root="repo")

    expected = str(workspace.resolve())
    assert session.workspace_root == expected
    assert not store.session_dir(session.id).exists()
    await manager.send_message(session.id, "hello")
    assert store.read_meta(session.id).workspace_root == expected
    assert events[0].workspace_root == expected  # type: ignore[attr-defined]


# 功能：验证不存在的工作区会以 invalid params 拒绝创建 session
# 设计：直接检查 HandlerError 错误码，确保 IPC 能返回可识别的客户端输入错误而非内部异常
async def test_create_session_rejects_missing_workspace(tmp_path: Path) -> None:
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: _Runner(),
        EventBus(),
    )  # type: ignore[arg-type]

    with pytest.raises(HandlerError) as exc:
        await manager.create("chat", workspace_root=str(tmp_path / "missing"))

    assert exc.value.code == INVALID_PARAMS


# 功能：验证已有聊天记录的未绑定 session 可以首次设置工作区并持久化
# 设计：先完成一轮消息再绑定目录，同时检查历史、meta 和事件，覆盖“聊几句后补 workspace”的主路径
async def test_set_workspace_after_messages_preserves_session_history(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    # 收集绑定前后的 session 事件以验证新增事件位于末尾
    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]
    session = await manager.create("chat")
    await manager.send_message(session.id, "先聊一句")
    workspace = tmp_path / "repo"
    workspace.mkdir()

    attached = await manager.set_workspace(session.id, str(workspace))

    expected = str(workspace.resolve())
    assert attached == expected
    assert store.read_meta(session.id).workspace_root == expected
    assert store.read_messages(session.id)[0]["content"] == "先聊一句"
    assert events[-1].type == "session.workspace_set"  # type: ignore[attr-defined]
    assert events[-1].workspace_root == expected  # type: ignore[attr-defined]


# 功能：验证 session 已绑定后只能幂等设置同一路径，不能切换到另一工作区
# 设计：连续设置相同和不同目录，分别断言成功与专用错误码，并确认持久化值未被覆盖
async def test_set_workspace_is_idempotent_but_rejects_switching(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")

    first_result = await manager.set_workspace(session.id, str(first))
    same_result = await manager.set_workspace(session.id, str(first))
    with pytest.raises(HandlerError) as exc:
        await manager.set_workspace(session.id, str(second))

    assert same_result == first_result
    assert exc.value.code == SESSION_WORKSPACE_ALREADY_SET
    assert not store.session_dir(session.id).exists()
    await manager.send_message(session.id, "hello")
    assert store.read_meta(session.id).workspace_root == str(first.resolve())


# 功能：验证 chat session 处理一条消息后进入 waiting_for_input，并保留 user/assistant thread
# 设计：mock runner 主动追加 assistant 消息，确认 send_message 负责 user 消息、状态流转和 run_id 记录
async def test_send_message_chat_enters_waiting_and_writes_thread(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")

    run_id = await manager.send_message(session.id, "hello")

    loaded = store.read_meta(session.id)
    assert loaded.status == "waiting_for_input"
    assert loaded.run_ids == [run_id]
    messages = store.read_messages(session.id)
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[1]["role"] == "assistant"


# 功能：验证成功 run 结束后仅生成 pending 候选，不直接写入 active memory
# 设计：覆盖 SessionManager 的自动提取时机和长期记忆与 thread.jsonl 的分离
async def test_successful_run_generates_pending_memory_candidate(tmp_path: Path) -> None:
    session_store = SessionStore(tmp_path / "sessions")
    memory_store = MemoryStore(tmp_path / "memory.db")
    manager = SessionManager(
        session_store,
        lambda: _Runner(),
        EventBus(),
        memory_store=memory_store,
        memory_generate_enabled=True,
    )  # type: ignore[arg-type]
    session = await manager.create("chat")

    await manager.send_message(session.id, "remember that I prefer concise answers.")

    assert memory_store.search("concise") == []
    candidates = memory_store.list_candidates(session_id=session.id)
    assert len(candidates) == 1
    assert candidates[0].status == "pending"
    assert len(session_store.read_messages(session.id)) == 2


# 功能：验证 one_shot session 在单次消息完成后自动 closed
# 设计：复用 mock runner 的成功路径，聚焦 mode 对最终状态的影响，保证 lite run 的统一路径正确
async def test_one_shot_auto_closes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("one_shot")

    await manager.send_message(session.id, "hello")

    assert store.read_meta(session.id).status == "closed"


# 功能：验证不存在的 session_id 返回 session_not_found 错误码
# 设计：直接调用 get_history 的查找路径，断言 HandlerError code，覆盖 IPC handler 可结构化返回错误
async def test_missing_session_raises_handler_error(tmp_path: Path) -> None:
    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    with pytest.raises(HandlerError) as exc:
        await manager.get_history("missing")
    assert exc.value.code == SESSION_NOT_FOUND


# 功能：验证 closed session 不能继续 send_message
# 设计：先显式 close，再发送消息，断言 session_closed 错误码，覆盖状态机拒绝路径
async def test_closed_session_rejects_message(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    await manager.send_message(session.id, "hello")
    await manager.close(session.id)

    with pytest.raises(HandlerError) as exc:
        await manager.send_message(session.id, "again")
    assert exc.value.code == SESSION_CLOSED


# 功能：验证磁盘中的已关闭 chat session 可恢复并切换到用户确认的新工作区
# 设计：用首个 manager 创建并关闭会话，再用全新 manager 恢复，覆盖 daemon 重启后的冷加载路径
async def test_resume_loads_closed_session_from_disk_and_switches_workspace(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    first = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await first.create("chat", workspace_root=str(first_workspace))
    await first.send_message(session.id, "remember this")
    last_chat_at = store.read_meta(session.id).updated_at
    assert store.read_meta(session.id).last_chat_at == last_chat_at
    await first.set_memory(session.id, generate_enabled=True)
    await first.set_ui_stats(session.id, {"rounds": 3, "steps": 9})
    await first.close(session.id)
    assert store.read_meta(session.id).updated_at == last_chat_at

    events: list[object] = []
    bus = EventBus()

    # 收集恢复事件以确认状态持久化和通知发生在同一管理操作中
    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    restarted = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]
    resumed = await restarted.resume(session.id, str(second_workspace))

    assert resumed.status == "waiting_for_input"
    assert resumed.workspace_root == str(second_workspace.resolve())
    assert resumed.updated_at == last_chat_at
    assert resumed.last_chat_at == last_chat_at
    assert resumed.ui_stats == {"rounds": 3, "steps": 9}
    assert store.read_meta(session.id).status == "waiting_for_input"
    assert store.read_meta(session.id).updated_at == last_chat_at
    assert await restarted.get_history(session.id) == store.read_messages(session.id)
    assert events[-1].type == "session.resumed"  # type: ignore[attr-defined]


# 功能：验证从未收到消息的 session 关闭时不会创建磁盘目录
# 设计：覆盖 TUI 退出和 /new 关闭空 session 的路径
async def test_close_unused_session_does_not_persist_directory(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    session_dir = store.session_dir(session.id)

    await manager.close(session.id)

    assert not session_dir.exists()


# 功能：验证旧版被 resume 改晚的 updated_at 会按 thread 最后一条消息时间自动修复
# 设计：构造未来元数据时间和更早的真实消息时间，列表返回值及持久化迁移字段都应采用消息时间
def test_list_sessions_migrates_last_chat_time_from_thread(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = Session(
        id="sess-20260815-000000-000000000088",
        mode="chat",
        status="closed",
        title="legacy",
        created_at="2026-08-15T00:00:00+00:00",
        updated_at="2026-08-28T09:28:00+00:00",
    )
    store.write_meta(session)
    store.append_message(session.id, "user", "real last chat")
    actual_chat_time = store.last_message_at(session.id)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]

    listed = manager.list_sessions()

    assert listed[0].updated_at == actual_chat_time
    assert store.read_meta(session.id).last_chat_at == actual_chat_time
