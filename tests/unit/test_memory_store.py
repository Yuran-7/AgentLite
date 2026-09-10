from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_lite.core.memory.model import MemoryExtractionItem, MemoryOperation
from agent_lite.core.memory.store import MemoryStore, workspace_id


def test_store_removes_legacy_candidate_table(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE memory_candidates (id TEXT PRIMARY KEY)")

    MemoryStore(database)

    with sqlite3.connect(database) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memory_candidates'"
        ).fetchone()
    assert table is None


def test_raw_item_is_pending_until_phase2_applies_operation(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    raw = store.save_raw_items(
        [
            MemoryExtractionItem(
                type="preference",
                scope="global",
                key="response.style",
                content="Answer concisely.",
                confidence=0.9,
                stability="stable",
            )
        ],
        session_id="sess-1",
        run_id="run-1",
        source_hash="hash-1",
    )

    assert store.search("concisely") == []
    assert store.list_raw_items(session_id="sess-1")[0].status == "pending"
    store.apply_memory_operations(
        raw,
        [
            MemoryOperation(
                action="add",
                target_key="response.style",
                type="preference",
                scope="global",
                content="Answer concisely.",
                confidence=0.9,
                importance=0.8,
            )
        ],
        session_id="sess-1",
        workspace_root=None,
    )

    assert store.search("concisely")[0].key == "response.style"
    assert store.list_raw_items(session_id="sess-1", status="processed")


def test_phase2_update_supersedes_old_record_and_delete_keeps_tombstone(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    first = store.save_raw_items(
        [
            MemoryExtractionItem(
                type="preference",
                scope="global",
                key="response.style",
                content="Answer concisely.",
                confidence=0.9,
            )
        ],
        session_id="sess-1",
        run_id="run-1",
        source_hash="hash-1",
    )
    store.apply_memory_operations(
        first,
        [
            MemoryOperation(
                action="add",
                target_key="response.style",
                type="preference",
                scope="global",
                content="Answer concisely.",
                confidence=0.9,
            )
        ],
        session_id="sess-1",
        workspace_root=None,
    )
    second = store.save_raw_items(
        [
            MemoryExtractionItem(
                type="preference",
                scope="global",
                key="response.style",
                content="Answer concisely and directly.",
                confidence=0.95,
            )
        ],
        session_id="sess-2",
        run_id="run-2",
        source_hash="hash-2",
    )
    store.apply_memory_operations(
        second,
        [
            MemoryOperation(
                action="update",
                target_key="response.style",
                type="preference",
                scope="global",
                content="Answer concisely and directly.",
                confidence=0.95,
            )
        ],
        session_id="sess-2",
        workspace_root=None,
    )

    visible = store.search("directly")
    assert len(visible) == 1
    old = store.list_memories(include_deleted=True, limit=10)
    assert any(memory.status == "superseded" for memory in old)
    assert store.delete(visible[0].id)
    assert store.get(visible[0].id).status == "deleted"  # type: ignore[union-attr]


def test_search_only_returns_visible_scopes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    workspace = tmp_path / "repo"
    workspace.mkdir()

    items = [
        ("global", None, "user.fact", "The user likes tea."),
        ("workspace", workspace_id(workspace), "project.database", "This workspace uses SQLite."),
        ("workspace", workspace_id(tmp_path), "project.database", "The other workspace uses Postgres."),
    ]
    for index, (scope, scoped_workspace, key, content) in enumerate(items):
        raw = store.save_raw_items(
            [
                MemoryExtractionItem(
                    type="fact" if scope == "global" else "decision",
                    scope=scope,  # type: ignore[arg-type]
                    key=key,
                    content=content,
                    confidence=0.9,
                )
            ],
            session_id=f"sess-{index}",
            run_id=f"run-{index}",
            source_hash=f"hash-{index}",
        )
        store.apply_memory_operations(
            raw,
            [
                MemoryOperation(
                    action="add",
                    target_key=key,
                    type="fact" if scope == "global" else "decision",
                    scope=scope,  # type: ignore[arg-type]
                    content=content,
                    confidence=0.9,
                )
            ],
            session_id=f"sess-{index}",
            workspace_root=workspace if scoped_workspace == workspace_id(workspace) else tmp_path,
        )

    visible = store.search("database", workspace_root=workspace, session_id="sess")
    assert [record.content for record in visible] == ["This workspace uses SQLite."]
    assert store.search("tea", workspace_root=workspace, session_id="sess")[0].scope == "global"
