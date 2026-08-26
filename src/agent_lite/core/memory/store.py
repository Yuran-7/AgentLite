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
    MemoryCandidate,
    MemoryRecord,
    MemoryScope,
    MemoryType,
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


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


class MemoryStore:
    """SQLite-backed long-term memory and pending candidate store.

    This store deliberately has no dependency on the LLM or SessionStore.  It
    can therefore be used from IPC handlers, session retrieval, and background
    candidate generation without putting memory data into thread.jsonl.
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
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    workspace_id TEXT,
                    session_id TEXT,
                    type TEXT NOT NULL,
                    key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    importance REAL NOT NULL,
                    evidence TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_candidates_status_idx
                    ON memory_candidates(status, created_at);
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
    def _candidate_from_row(row: sqlite3.Row) -> MemoryCandidate:
        return MemoryCandidate(
            id=row["id"],
            action=row["action"],
            scope=row["scope"],
            profile_id=row["profile_id"],
            workspace_id=row["workspace_id"],
            session_id=row["session_id"],
            type=row["type"],
            key=row["key"],
            content=row["content"],
            reason=row["reason"],
            confidence=float(row["confidence"]),
            importance=float(row["importance"]),
            evidence=row["evidence"],
            source=json.loads(row["source_json"]),
            tags=json.loads(row["tags_json"]),
            status=row["status"],
            created_at=row["created_at"],
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

    def create_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate:
        with self._connect() as db:
            existing = db.execute(
                """SELECT * FROM memory_candidates
                   WHERE status = 'pending' AND scope = ? AND profile_id = ?
                     AND key = ? AND content = ? AND workspace_id IS ? AND session_id IS ?
                   LIMIT 1""",
                (
                    candidate.scope,
                    candidate.profile_id,
                    candidate.key,
                    candidate.content,
                    candidate.workspace_id,
                    candidate.session_id,
                ),
            ).fetchone()
            if existing is not None:
                return self._candidate_from_row(existing)
            db.execute(
                """INSERT INTO memory_candidates(
                    id, action, scope, profile_id, workspace_id, session_id, type,
                    key, content, reason, confidence, importance, evidence,
                    source_json, tags_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate.id,
                    candidate.action,
                    candidate.scope,
                    candidate.profile_id,
                    candidate.workspace_id,
                    candidate.session_id,
                    candidate.type,
                    candidate.key,
                    candidate.content,
                    candidate.reason,
                    candidate.confidence,
                    candidate.importance,
                    candidate.evidence,
                    _json(candidate.source),
                    _json(candidate.tags),
                    candidate.status,
                    candidate.created_at,
                ),
            )
        return candidate

    def get_candidate(self, candidate_id: str) -> MemoryCandidate | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM memory_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            return self._candidate_from_row(row) if row is not None else None

    def list_candidates(
        self,
        *,
        session_id: str | None = None,
        status: str = "pending",
        limit: int = 50,
    ) -> list[MemoryCandidate]:
        query = "SELECT * FROM memory_candidates WHERE status = ?"
        args: list[Any] = [status]
        if session_id is not None:
            query += " AND (session_id = ? OR session_id IS NULL)"
            args.append(session_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        with self._connect() as db:
            return [self._candidate_from_row(row) for row in db.execute(query, args)]

    def _set_candidate_status(self, db: sqlite3.Connection, candidate_id: str, status: str) -> None:
        db.execute(
            "UPDATE memory_candidates SET status = ? WHERE id = ?", (status, candidate_id)
        )

    def commit_candidate(self, candidate_id: str) -> MemoryRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM memory_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                return None
            candidate = self._candidate_from_row(row)
            if candidate.status != "pending":
                if candidate.status == "accepted":
                    active = self._find_active_by_key(
                        db,
                        scope=candidate.scope,
                        profile_id=candidate.profile_id,
                        workspace_id_value=candidate.workspace_id,
                        session_id=candidate.session_id,
                        key=candidate.key,
                    )
                    return self._record_from_row(active) if active is not None else None
                return None
            if candidate.action in ("skip", "delete"):
                status = "rejected" if candidate.action == "skip" else "accepted"
                self._set_candidate_status(db, candidate.id, status)
                return None

            previous = self._find_active_by_key(
                db,
                scope=candidate.scope,
                profile_id=candidate.profile_id,
                workspace_id_value=candidate.workspace_id,
                session_id=candidate.session_id,
                key=candidate.key,
            )
            now = _now()
            record = MemoryRecord(
                id=f"mem_{uuid.uuid4().hex}",
                scope=candidate.scope,
                profile_id=candidate.profile_id,
                workspace_id=candidate.workspace_id,
                session_id=candidate.session_id,
                type=candidate.type,
                key=candidate.key,
                content=candidate.content,
                confidence=candidate.confidence,
                importance=candidate.importance,
                source=candidate.source,
                tags=candidate.tags,
                created_at=now,
                updated_at=now,
                supersedes=previous["id"] if previous is not None else None,
            )
            if previous is not None:
                db.execute(
                    "UPDATE memories SET status = 'superseded', updated_at = ?, "
                    "reason = ? WHERE id = ?",
                    (now, "replaced by newer memory", previous["id"]),
                )
                self._event(db, previous["id"], "superseded", {"by": record.id})
            db.execute(
                """INSERT INTO memories(
                    id, scope, profile_id, workspace_id, session_id, type, key, content,
                    status, confidence, importance, source_json, tags_json, created_at,
                    updated_at, expires_at, supersedes, deleted_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.scope,
                    record.profile_id,
                    record.workspace_id,
                    record.session_id,
                    record.type,
                    record.key,
                    record.content,
                    record.status,
                    record.confidence,
                    record.importance,
                    _json(record.source),
                    _json(record.tags),
                    record.created_at,
                    record.updated_at,
                    record.expires_at,
                    record.supersedes,
                    record.deleted_at,
                    record.reason,
                ),
            )
            self._sync_fts(db, record)
            event_type = "updated" if previous is not None else "created"
            self._event(db, record.id, event_type, record.model_dump())
            self._set_candidate_status(db, candidate.id, "accepted")
            return record

    def reject_candidate(self, candidate_id: str) -> bool:
        with self._connect() as db:
            updated = db.execute(
                "UPDATE memory_candidates SET status = 'rejected' "
                "WHERE id = ? AND status = 'pending'",
                (candidate_id,),
            ).rowcount
            return bool(updated)

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

    def generate_from_messages(
        self,
        messages: Iterable[dict[str, Any]],
        *,
        session_id: str | None,
        run_id: str | None,
        workspace_root: str | Path | None,
        max_candidates: int = 3,
    ) -> list[MemoryCandidate]:
        candidates: list[MemoryCandidate] = []
        seen: set[tuple[str, str]] = set()
        for message in messages:
            if message.get("role") != "user":
                continue
            text = _text_from_content(message.get("content", "")).strip()
            if not text or self._contains_sensitive_data(text):
                continue
            extracted = self._extract_explicit(text)
            for content, memory_type, key, scope, reason, confidence in extracted:
                identifier = (key, content)
                if identifier in seen or len(candidates) >= max_candidates:
                    continue
                seen.add(identifier)
                candidate = MemoryCandidate(
                    id=f"cand_{uuid.uuid4().hex}",
                    action="add",
                    scope=scope,
                    workspace_id=workspace_id(workspace_root) if scope != "global" else None,
                    session_id=session_id if scope == "session" else None,
                    type=memory_type,
                    key=key,
                    content=content,
                    reason=reason,
                    confidence=confidence,
                    importance=0.8 if scope != "session" else 0.5,
                    evidence=text[:500],
                    source={"session_id": session_id, "run_id": run_id},
                    tags=[memory_type],
                    created_at=_now(),
                )
                candidates.append(self.create_candidate(candidate))
        return candidates

    @staticmethod
    def _contains_sensitive_data(text: str) -> bool:
        return bool(re.search(
            r"(?i)(password|passwd|token|secret|api[_ -]?key|密钥|密码|口令)", text
        ))

    @staticmethod
    def _extract_explicit(
        text: str,
    ) -> list[tuple[str, MemoryType, str, MemoryScope, str, float]]:
        results: list[tuple[str, MemoryType, str, MemoryScope, str, float]] = []
        patterns = [
            r"(?:请)?记住(?:一下)?[：:\s]*(.+)$",
            r"remember(?: that)?[：:\s]+(.+)$",
            r"(?:以后|今后|后续)[，,：:\s]*(.+)$",
            r"from now on[，,：:\s]*(.+)$",
        ]
        captured: str | None = None
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                captured = match.group(1).strip().rstrip("。.!！")
                break
        if not captured or len(captured) < 2 or len(captured) > 500:
            return results

        preference = bool(
            re.search(r"喜欢|偏好|简短|简洁|语言|回答风格|prefer|like|style", captured, re.I)
        )
        decision = bool(
            re.search(
                r"项目|本项目|统一|采用|使用|决定|架构|package|database|project",
                captured,
                re.I,
            )
        )
        temporary = bool(
            re.search(
                r"本次|这次|当前任务|临时|this session|for this task|temporary",
                captured,
                re.I,
            )
        )
        memory_type: MemoryType
        key: str
        scope: MemoryScope
        reason: str
        if temporary:
            memory_type = "fact"
            key = "session.note"
            scope = "session"
            reason = "用户明确将这条约束限定在当前 Session。"
        elif preference:
            memory_type = "preference"
            key = (
                "response.style"
                if re.search(r"回答|简短|简洁|style", captured, re.I)
                else "user.preference"
            )
            scope = "global"
            reason = "用户明确表达了跨 Session 可复用的偏好。"
        elif decision:
            memory_type = "decision"
            key = (
                "project.database"
                if re.search(r"sqlite|数据库|database", captured, re.I)
                else "project.decision"
            )
            scope = "workspace"
            reason = "用户明确表达了项目级技术决策。"
        else:
            memory_type = "fact"
            key = "user.fact." + hashlib.sha256(captured.casefold().encode()).hexdigest()[:12]
            scope = "global"
            reason = "用户明确要求保留这条稳定事实。"
        results.append((captured, memory_type, key, scope, reason, 0.95))
        return results
