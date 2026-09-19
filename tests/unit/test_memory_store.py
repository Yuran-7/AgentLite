from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_lite.core.memory.store import MemoryStore, workspace_id


# 功能：验证初始化会移除旧候选表与两张已废弃的 Phase 1 表。
# 设计：先构造旧 schema，再初始化新 store 并检查 SQLite 的权威表清单。
def test_store_migrates_obsolete_phase1_tables(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE memory_candidates (id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE memory_raw_items (id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE memory_generation_runs (session_id TEXT PRIMARY KEY)")

    MemoryStore(database)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "memory_candidates" not in tables
    assert "memory_raw_items" not in tables
    assert "memory_generation_runs" not in tables
    assert "rollout_summary_sessions" in tables


# 功能：验证 rollout summary 文件与元数据可被覆盖更新而不产生重复文件。
# 设计：对同一 Session 保存两次并检查固定路径、最新内容和最新哈希。
def test_rollout_summary_uses_stable_session_path(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")

    first_path = store.save_rollout_summary(
        session_id="session-1", source_hash="hash-1", summary="# First"
    )
    second_path = store.save_rollout_summary(
        session_id="session-1", source_hash="hash-2", summary="# Second"
    )

    assert first_path == second_path == tmp_path / "rollout_summaries" / "session-1.md"
    assert second_path is not None
    assert second_path.read_text(encoding="utf-8") == "# Second\n"
    assert list(store.rollout_summaries_dir.glob("*.md")) == [second_path]
    metadata = store.get_rollout_summary_metadata("session-1")
    assert metadata is not None and metadata.source_hash == "hash-2"


# 功能：验证 Session ID 不能逃逸 rollout_summaries 目录。
# 设计：传入路径穿越字符串并断言在任何写入前被拒绝。
def test_rollout_summary_rejects_unsafe_session_id(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")

    with pytest.raises(ValueError, match="invalid session ID"):
        store.save_rollout_summary(
            session_id="../escape", source_hash="hash", summary="unsafe"
        )


# 功能：验证同一工作区路径始终映射为同一匿名标识。
# 设计：分别传入 Path 与字符串形式，排除调用类型造成的哈希差异。
def test_workspace_id_is_stable(tmp_path: Path) -> None:
    assert workspace_id(tmp_path) == workspace_id(str(tmp_path))
    assert workspace_id(None) is None
