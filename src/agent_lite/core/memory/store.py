from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_lite.core.memory.model import MemoryRecord, MemoryScope, RolloutSummaryMetadata


# 返回当前 UTC 时间的 ISO 8601 字符串。
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 为工作区绝对路径生成稳定且不泄露路径内容的标识。
def workspace_id(workspace_root: str | Path | None) -> str | None:
    if workspace_root is None or not str(workspace_root).strip():
        return None
    resolved = str(Path(workspace_root).expanduser().resolve())
    digest = hashlib.sha256(resolved.casefold().encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# 将值序列化为紧凑 JSON。
def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class MemoryStore:
    """SQLite metadata and filesystem artifacts for long-term memory."""

    # 初始化数据库及 rollout summary 目录。
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rollout_summaries_dir = self.path.parent / "rollout_summaries"
        self.rollout_summaries_dir.mkdir(parents=True, exist_ok=True)
        self._fts_available = False
        self._initialize()

    # 打开带事务管理的 SQLite 连接。
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    # 创建当前 schema，并移除已废弃的 Phase 1 原子项表。
    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    workspace_id TEXT,
                    session_id TEXT,
                    type TEXT NOT NULL,
                    key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    importance REAL NOT NULL,
                    source_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    supersedes TEXT,
                    deleted_at TEXT,
                    reason TEXT
                );
                CREATE INDEX IF NOT EXISTS memories_visibility_idx
                    ON memories(status, scope, profile_id, workspace_id, session_id);
                CREATE INDEX IF NOT EXISTS memories_key_idx
                    ON memories(scope, profile_id, workspace_id, session_id, key, status);
                CREATE TABLE IF NOT EXISTS memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rollout_summary_sessions (
                    session_id TEXT PRIMARY KEY,
                    source_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                DROP TABLE IF EXISTS memory_candidates;
                DROP TABLE IF EXISTS memory_raw_items;
                DROP TABLE IF EXISTS memory_generation_runs;
                """
            )
            try:
                db.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                        memory_id UNINDEXED, key, content, tags
                    )"""
                )
                self._fts_available = True
                count = db.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
                if count == 0:
                    rows = db.execute(
                        "SELECT id, key, content, tags_json FROM memories WHERE status = 'active'"
                    ).fetchall()
                    db.executemany(
                        "INSERT INTO memory_fts(memory_id, key, content, tags) VALUES (?, ?, ?, ?)",
                        [(row["id"], row["key"], row["content"], row["tags_json"]) for row in rows],
                    )
            except sqlite3.OperationalError:
                self._fts_available = False

    # 将数据库行转换为长期记忆模型。
    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            scope=row["scope"],
            profile_id=row["profile_id"],
            workspace_id=row["workspace_id"],
            session_id=row["session_id"],
            type=row["type"],
            key=row["key"],
            content=row["content"],
            status=row["status"],
            confidence=float(row["confidence"]),
            importance=float(row["importance"]),
            source=json.loads(row["source_json"]),
            tags=json.loads(row["tags_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            supersedes=row["supersedes"],
            deleted_at=row["deleted_at"],
            reason=row["reason"],
        )

    # 返回指定 Session 的稳定 rollout summary 路径。
    def rollout_summary_path(self, session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", session_id):
            raise ValueError(f"invalid session ID: {session_id!r}")
        return self.rollout_summaries_dir / f"{session_id}.md"

    # 判断 Session 当前内容是否已经生成过摘要。
    def is_rollout_summary_current(self, session_id: str, source_hash: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT source_hash FROM rollout_summary_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row is not None and row["source_hash"] == source_hash

    # 原子写入 rollout summary，并更新三列 Session 元数据。
    def save_rollout_summary(
        self, *, session_id: str, source_hash: str, summary: str
    ) -> Path | None:
        path = self.rollout_summary_path(session_id)
        content = summary.strip()
        if content:
            temporary = path.with_suffix(".md.tmp")
            temporary.write_text(content + "\n", encoding="utf-8")
            temporary.replace(path)
        with self._connect() as db:
            db.execute(
                """INSERT INTO rollout_summary_sessions(session_id, source_hash, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       source_hash = excluded.source_hash,
                       updated_at = excluded.updated_at""",
                (session_id, source_hash, _now()),
            )
        return path if content else None

    # 读取一条 rollout summary 的最小元数据。
    def get_rollout_summary_metadata(self, session_id: str) -> RolloutSummaryMetadata | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT session_id, source_hash, updated_at FROM rollout_summary_sessions "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return RolloutSummaryMetadata.model_validate(dict(row)) if row is not None else None

    # 记录长期记忆的审计事件。
    def _event(
        self, db: sqlite3.Connection, memory_id: str, event_type: str, payload: Any
    ) -> None:
        db.execute(
            "INSERT INTO memory_events(memory_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (memory_id, event_type, _json(payload), _now()),
        )

    # 软删除一条旧版 active memory，并保留 tombstone。
    def delete(self, memory_id: str, reason: str = "user_requested") -> bool:
        with self._connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None or row["status"] == "deleted":
                return False
            now = _now()
            db.execute(
                "UPDATE memories SET status = 'deleted', deleted_at = ?, "
                "updated_at = ?, reason = ? WHERE id = ?",
                (now, now, reason, memory_id),
            )
            if self._fts_available:
                db.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
            self._event(db, memory_id, "deleted", {"reason": reason})
            return True

    # 按 ID 读取一条旧版长期记忆。
    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return self._record_from_row(row) if row is not None else None

    # 列出满足作用域条件的旧版长期记忆。
    def list_memories(
        self,
        *,
        scope: MemoryScope | None = None,
        session_id: str | None = None,
        workspace_root: str | Path | None = None,
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        query = "SELECT * FROM memories WHERE 1 = 1"
        args: list[Any] = []
        if not include_deleted:
            query += " AND status = 'active'"
        if scope is not None:
            query += " AND scope = ?"
            args.append(scope)
        if session_id is not None:
            query += " AND (session_id = ? OR session_id IS NULL)"
            args.append(session_id)
        if workspace_root is not None:
            query += " AND (workspace_id = ? OR workspace_id IS NULL)"
            args.append(workspace_id(workspace_root))
        query += " ORDER BY updated_at DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with self._connect() as db:
            return [self._record_from_row(row) for row in db.execute(query, args)]

    # 用轻量词法匹配检索当前调用方可见的旧版长期记忆。
    def search(
        self,
        query: str,
        *,
        profile_id: str = "default",
        workspace_root: str | Path | None = None,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[MemoryRecord]:
        current_workspace = workspace_id(workspace_root)
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM memories
                   WHERE status = 'active' AND profile_id = ?
                     AND (scope = 'global'
                          OR (scope = 'workspace' AND workspace_id = ?)
                          OR (scope = 'session' AND session_id = ?))""",
                (profile_id, current_workspace, session_id),
            ).fetchall()
        tokens = [
            token.casefold()
            for token in re.findall(r"[\w\u4e00-\u9fff]+", query)
            if token.strip()
        ]
        scored: list[tuple[float, MemoryRecord]] = []
        for row in rows:
            record = self._record_from_row(row)
            haystack = f"{record.key} {record.content} {' '.join(record.tags)}".casefold()
            lexical = sum(1.0 for token in tokens if token in haystack)
            if tokens and lexical == 0:
                continue
            scope_bonus = {"session": 3.0, "workspace": 2.0, "global": 1.0}[record.scope]
            score = lexical * 10 + scope_bonus + record.importance + record.confidence
            scored.append((score, record))
        scored.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
        return [record for _, record in scored[: max(1, min(limit, 50))]]

    # 将相关旧版长期记忆格式化为可注入模型的上下文。
    def format_relevant(
        self,
        query: str,
        *,
        workspace_root: str | Path | None = None,
        session_id: str | None = None,
        max_items: int = 5,
        max_chars: int = 2000,
    ) -> str:
        records = self.search(
            query,
            workspace_root=workspace_root,
            session_id=session_id,
            limit=max_items,
        )
        if not records:
            return ""
        lines = [
            "## Relevant memories",
            "",
            "Treat these as fallible context. The current user request has priority.",
            "",
        ]
        used = sum(len(line) + 1 for line in lines)
        for record in records:
            source_text = ", ".join(f"{key}={value}" for key, value in record.source.items())
            line = f"- [{record.scope}][{record.type}][{record.key}] {record.content}"
            if source_text:
                line += f" (source: {source_text})"
            if used + len(line) + 1 > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    # 读取上一次成功发布的紧凑记忆，供每轮 system prompt 注入。
    def format_memory_summary(self, *, max_chars: int = 10_000) -> str:
        path = self.path.parent / "memory_summary.md"
        if not path.is_file():
            return ""
        content = path.read_text(encoding="utf-8").strip()
        if content.splitlines()[:1] != ["v1"]:
            return ""
        bounded = content[: max(1, max_chars)]
        return (
            "## Long-term memory\n\n"
            "Treat this as fallible historical context. The current user request and "
            "workspace evidence take priority.\n\n"
            + bounded
        )
