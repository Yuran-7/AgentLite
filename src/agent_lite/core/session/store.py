from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_lite.core.session.ids import session_date_parts
from agent_lite.core.session.model import Session

logger = logging.getLogger(__name__)

MessageContent = str | list[dict[str, Any]]


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionStore:
    # 初始化 session 文件存储根目录
    def __init__(self, root: Path) -> None:
        self._root = root.expanduser()
        self._root.mkdir(parents=True, exist_ok=True)

    # 返回指定 session 的目录路径
    def session_dir(self, sid: str) -> Path:
        date_parts = session_date_parts(sid)
        if date_parts is None:
            raise ValueError(f"invalid session ID: {sid!r}")
        return self._root.joinpath(*date_parts, sid)

    # 返回指定 session 汇总所有 run 事件的日志路径
    def events_file(self, sid: str) -> Path:
        return self.session_dir(sid) / "events.jsonl"

    # 将 session meta 写入 meta.json
    def write_meta(self, session: Session) -> None:
        path = self.session_dir(session.id)
        path.mkdir(parents=True, exist_ok=True)
        (path / "meta.json").write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # 从 meta.json 读取 session meta
    def read_meta(self, sid: str) -> Session:
        data = json.loads((self.session_dir(sid) / "meta.json").read_text(encoding="utf-8"))
        return Session.from_dict(data)

    # 扫描磁盘中的 chat session，可按规范化工作区筛选并按最近更新时间倒序返回
    def list_sessions(self, workspace_root: str | None = None) -> list[Session]:
        expected_workspace = self._workspace_key(workspace_root)
        sessions: list[Session] = []
        for meta_path in self._root.glob("*/*/*/*/meta.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                session = Session.from_dict(data)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                logger.warning("skip broken session meta path=%s", meta_path)
                continue
            if session.mode != "chat":
                continue
            if (
                expected_workspace is not None
                and self._workspace_key(session.workspace_root) != expected_workspace
            ):
                continue
            sessions.append(session)
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    # 返回 thread 中最后一条有效消息的时间，用于迁移旧版 session 的最后聊天时间
    def last_message_at(self, sid: str) -> str | None:
        thread_path = self.session_dir(sid) / "thread.jsonl"
        if not thread_path.exists():
            return None
        try:
            lines = thread_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp = row.get("ts")
            if isinstance(timestamp, str) and timestamp:
                return timestamp
        return None

    # 将工作区转换为适合当前平台比较的绝对路径键
    @staticmethod
    def _workspace_key(workspace_root: str | None) -> str | None:
        if workspace_root is None or not workspace_root.strip():
            return None
        return os.path.normcase(str(Path(workspace_root).expanduser().resolve(strict=False)))

    # 追加一条 Anthropic API 消息到 thread.jsonl
    def append_message(
        self,
        sid: str,
        role: str,
        content: MessageContent,
        run_id: str | None = None,
    ) -> None:
        row: dict[str, Any] = {"ts": _now(), "role": role, "content": content}
        if run_id is not None:
            row["run_id"] = run_id
        path = self.session_dir(sid)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "thread.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 批量追加一次 run 新产生的消息到 thread.jsonl
    def append_messages(
        self,
        sid: str,
        messages: list[dict[str, Any]],
        run_id: str,
    ) -> None:
        for msg in messages:
            self.append_message(
                sid,
                role=str(msg["role"]),
                content=msg["content"],
                run_id=run_id,
            )

    # 读取完整 thread 并返回可直接传给 Anthropic 的 messages
    def read_messages(self, sid: str) -> list[dict[str, Any]]:
        path = self.session_dir(sid) / "thread.jsonl"
        if not path.exists():
            return []

        messages: list[dict[str, Any]] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skip broken thread row sid=%s line=%s", sid, line_no)
                continue
            role = row.get("role")
            if role not in ("user", "assistant"):
                logger.warning(
                    "skip unknown thread role sid=%s line=%s role=%s",
                    sid,
                    line_no,
                    role,
                )
                continue
            messages.append({"role": role, "content": row.get("content", "")})

        messages = self._trim_orphan_tool_use(messages)
        from agent_lite.core.compact.budget import truncate_tool_results
        return truncate_tool_results(messages)

    # 裁掉尾部未配对 tool_use 以及其后的消息，避免 Anthropic messages.invalid
    def _trim_orphan_tool_use(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: set[str] = set()
        last_balanced = 0
        for idx, msg in enumerate(messages, start=1):
            content = msg.get("content")
            if isinstance(content, list):
                if msg.get("role") == "assistant":
                    for block in content:
                        if block.get("type") == "tool_use":
                            pending.add(str(block.get("id", "")))
                elif msg.get("role") == "user":
                    for block in content:
                        if block.get("type") == "tool_result":
                            pending.discard(str(block.get("tool_use_id", "")))
            if not pending:
                last_balanced = idx
        if pending:
            logger.warning("trim orphan tool_use blocks from thread")
            return messages[:last_balanced]
        return messages

    # 将压缩后的消息对覆盖写入 thread.jsonl，原文件备份为 thread_<ts>.jsonl.bak
    def write_compacted(self, sid: str, messages: list[dict[str, Any]]) -> None:
        path = self.session_dir(sid) / "thread.jsonl"
        ts_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        bak = self.session_dir(sid) / f"thread_{ts_str}.jsonl.bak"
        if path.exists():
            path.rename(bak)
        with path.open("w", encoding="utf-8") as f:
            for msg in messages:
                row: dict[str, Any] = {"ts": _now(), "role": msg["role"], "content": msg["content"]}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
