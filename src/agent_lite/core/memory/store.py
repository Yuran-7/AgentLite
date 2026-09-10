from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from agent_lite.core.memory.model import (
    MemoryExtractionItem,
    MemoryOperation,
    MemoryRecord,
    MemoryScope,
    RawMemoryItem,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def workspace_id(workspace_root: str | Path | None) -> str | None:
    if workspace_root is None or not str(workspace_root).strip():
        return None
    resolved = str(Path(workspace_root).expanduser().resolve())
    digest = hashlib.sha256(resolved.casefold().encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class MemoryStore:
    """SQLite-backed long-term memory and Phase 1 raw-item store.

    This store deliberately has no dependency on the LLM or SessionStore.  It
    can therefore be used from IPC handlers, session retrieval, and background
    consolidation without putting memory data into thread.jsonl.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fts_available = False
        self._initialize()

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
                CREATE TABLE IF NOT EXISTS memory_raw_items (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    stability TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    processed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS memory_raw_items_status_idx
                    ON memory_raw_items(status, created_at);
                CREATE INDEX IF NOT EXISTS memory_raw_items_source_idx
                    ON memory_raw_items(session_id, source_hash);
                CREATE TABLE IF NOT EXISTS memory_generation_runs (
                    session_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                DROP TABLE IF EXISTS memory_candidates;
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
                        [
                            (row["id"], row["key"], row["content"], row["tags_json"])
                            for row in rows
                        ],
                    )
            except sqlite3.OperationalError:
                self._fts_available = False

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

    @staticmethod
    def _raw_from_row(row: sqlite3.Row) -> RawMemoryItem:
        return RawMemoryItem(
            id=row["id"],
            session_id=row["session_id"],
            run_id=row["run_id"],
            type=row["type"],
            scope=row["scope"],
            key=row["key"],
            content=row["content"],
            evidence=row["evidence"],
            confidence=float(row["confidence"]),
            stability=row["stability"],
            summary=row["summary"],
            source_hash=row["source_hash"],
            status=row["status"],
            created_at=row["created_at"],
            processed_at=row["processed_at"],
        )

    def _event(
        self, db: sqlite3.Connection, memory_id: str, event_type: str, payload: Any
    ) -> None:
        db.execute(
            "INSERT INTO memory_events(memory_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (memory_id, event_type, _json(payload), _now()),
        )

    def _sync_fts(self, db: sqlite3.Connection, record: MemoryRecord) -> None:
        if not self._fts_available:
            return
        db.execute("DELETE FROM memory_fts WHERE memory_id = ?", (record.id,))
        if record.status == "active":
            db.execute(
                "INSERT INTO memory_fts(memory_id, key, content, tags) VALUES (?, ?, ?, ?)",
                (record.id, record.key, record.content, " ".join(record.tags)),
            )

    def _scope_values(
        self,
        scope: MemoryScope,
        profile_id: str,
        workspace_root: str | Path | None,
        session_id: str | None,
    ) -> tuple[str, str | None, str | None]:
        if scope == "global":
            return profile_id, None, None
        if scope == "workspace":
            return profile_id, workspace_id(workspace_root), None
        return profile_id, workspace_id(workspace_root), session_id

    def _find_active_by_key(
        self,
        db: sqlite3.Connection,
        *,
        scope: MemoryScope,
        profile_id: str,
        workspace_id_value: str | None,
        session_id: str | None,
        key: str,
    ) -> sqlite3.Row | None:
        return cast(sqlite3.Row | None, db.execute(
            """SELECT * FROM memories
               WHERE status = 'active' AND scope = ? AND profile_id = ?
                 AND key = ? AND workspace_id IS ? AND session_id IS ?
               ORDER BY updated_at DESC LIMIT 1""",
            (scope, profile_id, key, workspace_id_value, session_id),
        ).fetchone())

    def claim_memory_generation(self, session_id: str, run_id: str, source_hash: str) -> bool:
        """Claim the first successful extraction slot for a session."""

        now = _now()
        with self._connect() as db:
            row = db.execute(
                "SELECT status FROM memory_generation_runs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is not None and row["status"] in {"running", "succeeded"}:
                return False
            if row is None:
                db.execute(
                    "INSERT INTO memory_generation_runs "
                    "(session_id, run_id, source_hash, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'running', ?, ?)",
                    (session_id, run_id, source_hash, now, now),
                )
            else:
                db.execute(
                    "UPDATE memory_generation_runs SET run_id = ?, source_hash = ?, "
                    "status = 'running', updated_at = ? WHERE session_id = ?",
                    (run_id, source_hash, now, session_id),
                )
            return True

    def finish_memory_generation(self, session_id: str, run_id: str, status: str) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError("invalid memory generation status")
        with self._connect() as db:
            db.execute(
                "UPDATE memory_generation_runs SET status = ?, updated_at = ? "
                "WHERE session_id = ? AND run_id = ?",
                (status, _now(), session_id, run_id),
            )

    def save_raw_items(
        self,
        items: Iterable[MemoryExtractionItem],
        *,
        session_id: str,
        run_id: str,
        source_hash: str,
        summary: str = "",
    ) -> list[RawMemoryItem]:
        """Persist Phase 1 output, deduplicated by session/source/key/content."""

        saved: list[RawMemoryItem] = []
        now = _now()
        with self._connect() as db:
            for item in items:
                content = item.content.strip()
                if not content or len(content) > 1_500 or self._contains_sensitive_data(content):
                    continue
                key = item.key.strip()[:200]
                if not key:
                    key = (
                        "memory."
                        + hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()[:16]
                    )
                existing = db.execute(
                    "SELECT * FROM memory_raw_items WHERE session_id = ? AND source_hash = ? "
                    "AND key = ? AND content = ? LIMIT 1",
                    (session_id, source_hash, key, content),
                ).fetchone()
                if existing is not None:
                    saved.append(self._raw_from_row(existing))
                    continue
                raw = RawMemoryItem(
                    id=f"raw_{uuid.uuid4().hex}",
                    session_id=session_id,
                    run_id=run_id,
                    type=item.type,
                    scope=item.scope,
                    key=key,
                    content=content,
                    evidence=item.evidence[:1_500],
                    confidence=item.confidence,
                    stability=item.stability,
                    summary=summary[:2_000],
                    source_hash=source_hash,
                    created_at=now,
                )
                db.execute(
                    "INSERT INTO memory_raw_items "
                    "(id, session_id, run_id, type, scope, key, content, evidence, confidence, "
                    "stability, summary, source_hash, status, created_at, processed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        raw.id, raw.session_id, raw.run_id, raw.type, raw.scope, raw.key,
                        raw.content, raw.evidence, raw.confidence, raw.stability, raw.summary,
                        raw.source_hash, raw.status, raw.created_at, raw.processed_at,
                    ),
                )
                saved.append(raw)
        return saved

    def list_raw_items(
        self,
        *,
        session_id: str | None = None,
        status: str = "pending",
        limit: int = 100,
    ) -> list[RawMemoryItem]:
        query = "SELECT * FROM memory_raw_items WHERE status = ?"
        args: list[Any] = [status]
        if session_id is not None:
            query += " AND session_id = ?"
            args.append(session_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with self._connect() as db:
            return [self._raw_from_row(row) for row in db.execute(query, args)]

    def apply_memory_operations(
        self,
        raw_items: Iterable[RawMemoryItem],
        operations: Iterable[MemoryOperation],
        *,
        session_id: str,
        workspace_root: str | Path | None,
    ) -> int:
        """Validate and apply Phase 2 operations in one SQLite transaction."""

        raw_list = list(raw_items)
        raw_by_key = {item.key: item for item in raw_list}
        applied = 0
        now = _now()
        with self._connect() as db:
            for operation in operations:
                op = (
                    operation
                    if isinstance(operation, MemoryOperation)
                    else MemoryOperation.model_validate(operation)
                )
                if not op.target_key or len(op.target_key) > 200:
                    continue
                if op.action in {"add", "update"}:
                    if op.type is None or op.scope is None:
                        continue
                    content = op.content.strip()
                    if (
                        not content
                        or len(content) > 1_500
                        or self._contains_sensitive_data(content)
                    ):
                        continue
                else:
                    content = op.content.strip()

                scope = op.scope
                if scope is None:
                    # delete/skip can inherit scope from the matching raw item only for
                    # exact-key operations; no inherited scope is allowed for writes.
                    continue
                profile, ws_id, scoped_session = self._scope_values(
                    scope, "default", workspace_root, session_id
                )
                if scope == "workspace" and ws_id is None:
                    continue
                if scope == "session" and scoped_session is None:
                    continue
                existing = self._find_active_by_key(
                    db,
                    scope=scope,
                    profile_id=profile,
                    workspace_id_value=ws_id,
                    session_id=scoped_session,
                    key=op.target_key,
                )
                if op.action == "skip":
                    applied += 1
                    continue
                if op.action == "delete":
                    if existing is None:
                        continue
                    db.execute(
                        "UPDATE memories SET status = 'deleted', deleted_at = ?, "
                        "updated_at = ?, reason = ? WHERE id = ?",
                        (now, now, op.reason or "consolidation", existing["id"]),
                    )
                    if self._fts_available:
                        db.execute("DELETE FROM memory_fts WHERE memory_id = ?", (existing["id"],))
                    self._event(db, existing["id"], "deleted", {"reason": op.reason})
                    applied += 1
                    continue
                if op.action == "update" and existing is None:
                    continue

                assert op.type is not None

                source_item = raw_by_key.get(op.target_key) or (raw_list[0] if raw_list else None)
                source: dict[str, Any] = {
                    "session_id": session_id,
                    "source": "memory_phase2",
                }
                if source_item is not None:
                    source.update(
                        run_id=source_item.run_id,
                        source_hash=source_item.source_hash,
                        evidence=op.evidence or source_item.evidence,
                    )
                record = MemoryRecord(
                    id=f"mem_{uuid.uuid4().hex}",
                    scope=scope,
                    profile_id=profile,
                    workspace_id=ws_id,
                    session_id=scoped_session,
                    type=op.type,
                    key=op.target_key,
                    content=content,
                    confidence=op.confidence,
                    importance=op.importance,
                    source=source,
                    tags=op.tags,
                    created_at=existing["created_at"] if existing is not None else now,
                    updated_at=now,
                    supersedes=existing["id"] if existing is not None else None,
                    reason=op.reason or None,
                )
                if existing is not None:
                    db.execute(
                        "UPDATE memories SET status = 'superseded', updated_at = ?, "
                        "reason = ? WHERE id = ?",
                        (now, "replaced by newer memory", existing["id"]),
                    )
                    superseded = self._record_from_row(existing).model_copy(
                        update={"status": "superseded"}
                    )
                    self._sync_fts(db, superseded)
                    self._event(db, existing["id"], "superseded", {"by": record.id})
                db.execute(
                    "INSERT INTO memories "
                    "(id, scope, profile_id, workspace_id, session_id, type, key, content, status, "
                    "confidence, importance, source_json, tags_json, created_at, updated_at, "
                    "expires_at, supersedes, deleted_at, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.id, record.scope, record.profile_id, record.workspace_id,
                        record.session_id, record.type, record.key, record.content, record.status,
                        record.confidence,
                        record.importance,
                        _json(record.source),
                        _json(record.tags),
                        record.created_at, record.updated_at, record.expires_at, record.supersedes,
                        record.deleted_at, record.reason,
                    ),
                )
                self._sync_fts(db, record)
                self._event(
                    db,
                    record.id,
                    "updated" if existing is not None else "created",
                    record.model_dump(),
                )
                applied += 1

            if raw_list:
                placeholders = ",".join("?" for _ in raw_list)
                db.execute(
                    f"UPDATE memory_raw_items SET status = 'processed', processed_at = ? "
                    f"WHERE id IN ({placeholders})",
                    [now, *(item.id for item in raw_list)],
                )
        return applied

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

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return self._record_from_row(row) if row is not None else None

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
            source = record.source
            source_text = ", ".join(f"{key}={value}" for key, value in source.items())
            line = f"- [{record.scope}][{record.type}][{record.key}] {record.content}"
            if source_text:
                line += f" (source: {source_text})"
            if used + len(line) + 1 > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    @staticmethod
    def _contains_sensitive_data(text: str) -> bool:
        return bool(re.search(
            r"(?i)(password|passwd|token|secret|api[_ -]?key|密钥|密码|口令)", text
        ))
