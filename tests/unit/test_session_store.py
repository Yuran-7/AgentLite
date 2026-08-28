from __future__ import annotations

from pathlib import Path

import pytest

from agent_lite.core.session.ids import new_session_id, session_date_parts
from agent_lite.core.session.model import Session
from agent_lite.core.session.store import SessionStore

SESSION_ID = "sess-20260815-000000-0123456789ab"


# 功能：验证 SessionStore 初始化时自动创建 sessions 根目录
# 设计：传入 tmp_path 下不存在的目录，断言目录被创建，覆盖首次启动 daemon 的冷路径
def test_store_creates_root(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    SessionStore(root)
    assert root.exists()


def test_new_session_id_contains_timestamp_and_random_suffix() -> None:
    session_id = new_session_id()

    assert session_id.startswith("sess-")
    assert session_date_parts(session_id) is not None


def test_timestamped_session_is_stored_under_date_partitions(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    store = SessionStore(root)
    session_id = "sess-20260815-092151-0123456789ab"
    session = Session(
        id=session_id,
        mode="chat",
        status="active",
        title="dated",
        created_at="2026-08-15T09:21:51+00:00",
        updated_at="2026-08-15T09:21:51+00:00",
    )

    store.write_meta(session)

    expected = root / "2026" / "08" / "15" / session_id / "meta.json"
    assert expected.exists()
    assert store.read_meta(session_id) == session


def test_store_rejects_legacy_session_id(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")

    with pytest.raises(ValueError, match="invalid session ID"):
        store.session_dir("sess-9a10f6855f03")


# 功能：验证 session meta 写入后能完整读回
# 设计：构造含 run_ids 的 Session，经过 JSON 文件往返后断言字段保持，覆盖 meta.json 的持久化契约
def test_meta_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = Session(
        id=SESSION_ID,
        mode="chat",
        status="waiting_for_input",
        title="hello",
        created_at="t1",
        updated_at="t2",
        workspace_root=str(tmp_path),
        run_ids=["run-1"],
    )
    store.write_meta(session)
    loaded = store.read_meta(SESSION_ID)
    assert loaded == session


# 功能：验证旧版 meta.json 缺少 workspace_root 时仍能恢复为无工作区 session
# 设计：直接调用向后兼容反序列化路径，避免老会话在升级后因新增字段无法读取
def test_meta_without_workspace_is_backward_compatible() -> None:
    session = Session.from_dict(
        {
            "id": "sess-old",
            "mode": "chat",
            "status": "closed",
            "title": "old",
            "created_at": "t1",
            "updated_at": "t2",
            "run_ids": [],
        }
    )

    assert session.workspace_root is None
    assert session.last_chat_at is None


# 功能：验证历史列表只返回 chat session，并支持按工作区筛选和最近更新时间排序
# 设计：写入跨工作区的两个 chat 与一个 one-shot，断言全量和目录过滤共享同一扫描结果
def test_list_sessions_filters_workspace_and_sorts_recent_first(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    sessions = [
        Session(
            id="sess-20260815-000000-000000000001",
            mode="chat",
            status="closed",
            title="older",
            created_at="2026-08-15T00:00:00+00:00",
            updated_at="2026-08-15T00:00:01+00:00",
            workspace_root=str(first_workspace),
        ),
        Session(
            id="sess-20260815-000000-000000000002",
            mode="chat",
            status="waiting_for_input",
            title="newer",
            created_at="2026-08-15T00:00:00+00:00",
            updated_at="2026-08-15T00:00:02+00:00",
            workspace_root=str(second_workspace),
        ),
        Session(
            id="sess-20260815-000000-000000000003",
            mode="one_shot",
            status="closed",
            title="hidden",
            created_at="2026-08-15T00:00:00+00:00",
            updated_at="2026-08-15T00:00:03+00:00",
        ),
    ]
    for session in sessions:
        store.write_meta(session)

    assert [session.title for session in store.list_sessions()] == ["newer", "older"]
    assert [
        session.title for session in store.list_sessions(str(first_workspace.resolve()))
    ] == ["older"]


# 功能：验证含 tool_use/tool_result block 的 thread 消息能按 Anthropic 格式读回
# 设计：追加 assistant tool_use 和 user tool_result，读取时应剥离 ts/run_id，只保留 API messages 所需字段
def test_thread_message_roundtrip_with_tool_blocks(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message(SESSION_ID, "user", "read file")
    store.append_message(
        SESSION_ID,
        "assistant",
        [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}],
        run_id="run-1",
    )
    store.append_message(
        SESSION_ID,
        "user",
        [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        run_id="run-1",
    )

    messages = store.read_messages(SESSION_ID)
    assert messages == [
        {"role": "user", "content": "read file"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
    ]


# 功能：验证 thread 尾部孤儿 tool_use 会被裁掉
# 设计：构造一条未配对 tool_result 的 assistant tool_use，读取时只返回最后一次配平之前的消息，避免 API 报 messages.invalid
def test_read_messages_trims_orphan_tool_use_tail(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message(SESSION_ID, "user", "hello")
    store.append_message(
        SESSION_ID,
        "assistant",
        [{"type": "tool_use", "id": "orphan", "name": "read_file", "input": {}}],
        run_id="run-1",
    )
    assert store.read_messages(SESSION_ID) == [{"role": "user", "content": "hello"}]


# 功能：验证存储层能从 thread 最后一条有效记录恢复旧 session 的最后聊天时间
# 设计：在有效消息后追加损坏行，反向扫描应跳过损坏内容并返回最后一个有效 ts
def test_last_message_at_skips_broken_tail(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message(SESSION_ID, "user", "hello")
    thread_path = store.session_dir(SESSION_ID) / "thread.jsonl"
    with thread_path.open("a", encoding="utf-8") as file:
        file.write("broken json\n")

    timestamp = store.last_message_at(SESSION_ID)

    assert timestamp is not None
    assert timestamp.endswith("+00:00")
