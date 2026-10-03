from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import stat
import tempfile
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import portalocker

MAX_FILE_BYTES = 1024 * 1024
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCK_GUARD = threading.Lock()


class FileOperationError(ValueError):
    # 保存不可自动重试的文件操作错误类型
    def __init__(self, message: str, error_type: str = "file_conflict") -> None:
        super().__init__(message)
        self.error_type = error_type


# 计算原始字节的版本，不使用可被保留或回拨的时间戳判断一致性
def version(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


# 生成规范路径的跨平台比较键
def path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


# Windows 保存 DACL，避免原子替换改变原文件的访问权限
def read_acl(path: Path) -> str | None:
    if os.name != "nt":
        return None
    import win32security  # type: ignore[import-untyped]

    descriptor = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    return str(
        win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
            descriptor, win32security.SDDL_REVISION_1, win32security.DACL_SECURITY_INFORMATION
        )
    )


# 发布前恢复 DACL 及继承保护标志，权限复制失败时中止提交
def apply_acl(path: Path, acl: str | None) -> None:
    if os.name != "nt" or acl is None:
        return
    import win32security

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        acl, win32security.SDDL_REVISION_1
    )
    control, _ = descriptor.GetSecurityDescriptorControl()
    inheritance = (
        win32security.PROTECTED_DACL_SECURITY_INFORMATION
        if control & win32security.SE_DACL_PROTECTED
        else win32security.UNPROTECTED_DACL_SECURITY_INFORMATION
    )
    win32security.SetFileSecurity(
        str(path), win32security.DACL_SECURITY_INFORMATION | inheritance, descriptor
    )


@contextmanager
# 在工作区之外使用线程锁和操作系统文件锁串行化同一目标的提交
def file_lock(path: Path) -> Iterator[None]:
    key = path_key(path)
    with _LOCK_GUARD:
        lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    directory = Path(tempfile.gettempdir()) / "agentlite-file-locks"
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
    with lock:
        process_lock = portalocker.Lock(str(lock_path), timeout=120)
        try:
            process_lock.acquire()
        except portalocker.exceptions.LockException as exc:
            raise FileOperationError(
                "File lock unavailable; retry after the other commit finishes"
            ) from exc
        try:
            yield
        finally:
            process_lock.release()


# 等待开始执行的提交结束后返回真实结果，避免取消后仍有后台写入
async def finish_commit[T](action: Callable[[], T]) -> T:
    task = asyncio.create_task(asyncio.to_thread(action))
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


# 在同目录写入完整字节并原子发布，创建时不覆盖抢先出现的文件
def atomic_bytes(path: Path, raw: bytes, mode: int | None = None, *, create: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".agentlite-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            temporary.chmod(mode)
        if create:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# 生成限量差异摘要，完整字节另存于历史备份
def text_diff(
    before: bytes | None, after: bytes | None, path: str, *, max_chars: int | None = 32_000,
) -> str:
    old = (before or b"").decode("utf-8-sig", errors="replace").splitlines(keepends=True)
    new = (after or b"").decode("utf-8-sig", errors="replace").splitlines(keepends=True)
    diff = "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in difflib.unified_diff(old, new, fromfile=f"a/{path}", tofile=f"b/{path}")
    )
    if max_chars is None:
        return diff
    return diff[:max_chars] + ("\n[diff truncated]" if len(diff) > max_chars else "")


@dataclass
class ReadState:
    digest: str
    total: int
    spans: list[tuple[int, int]] = field(default_factory=list)
    full_spans: list[tuple[int, int]] = field(default_factory=list)
    queries: dict[str, str | None] = field(default_factory=dict)

    # 合并已交付区间并验证目标区间完全可见
    def covers(self, start: int, end: int, *, full: bool = False) -> bool:
        intervals = self.full_spans if full else self.spans
        if start == end:
            return bool(intervals)
        cursor = start
        for left, right in sorted(intervals):
            if left > cursor:
                break
            cursor = max(cursor, right)
        return cursor >= end


# 合并相邻或重叠字符区间，使跨行替换后区间边界保持一致
def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


class FileReadContext:
    # 隔离一个 Agent 的实际阅读资格和去重结果
    def __init__(self) -> None:
        self.states: dict[str, ReadState] = {}
        self.epoch = 0
        self.prefix_length = 0
        self.prefix_digest: str | None = None

    # 压缩或上下文切换时清除全部资格，使未变化文件也重新交付正文
    def clear(self) -> None:
        self.states.clear()
        self.epoch += 1
        self.prefix_length = 0
        self.prefix_digest = None

    # 检查回放前缀是否仍含模型原先看到的内容，迁移截断时必须失效
    def verify_replay(self, messages: list[dict[str, Any]]) -> None:
        if self.prefix_digest is not None:
            raw = json.dumps(messages[: self.prefix_length], sort_keys=True).encode()
            if len(messages) < self.prefix_length or version(raw) != self.prefix_digest:
                self.clear()

    # 记录本轮实际上下文的稳定摘要，不额外缓存正文
    def remember_messages(self, messages: list[dict[str, Any]]) -> None:
        self.prefix_length = len(messages)
        self.prefix_digest = version(json.dumps(messages, sort_keys=True).encode())

    # 仅在模型上下文成功接收结果后确认读取，预览不能提供阅读资格
    def delivered(
        self,
        path: Path,
        digest: str,
        total: int,
        query: str,
        spans: list[tuple[int, int]],
        full_eligible: bool,
        truncated: bool,
        output_path: str | None,
        epoch: int,
    ) -> None:
        if epoch != self.epoch:
            return
        key = path_key(path)
        state = self.states.get(key)
        if state is None or state.digest != digest:
            state = ReadState(digest, total)
            self.states[key] = state
        state.queries[query] = output_path
        if not truncated:
            state.spans = merge_spans([*state.spans, *spans])
            if full_eligible:
                state.full_spans = merge_spans([*state.full_spans, *spans])


_SESSION_CONTEXTS: OrderedDict[str, tuple[str, FileReadContext]] = OrderedDict()


# 同一会话和同一系统上下文跨轮次复用阅读状态，缓存只存在当前守护进程
def session_read_context(directory: Path, scope: str) -> FileReadContext:
    key = path_key(directory)
    entry = _SESSION_CONTEXTS.get(key)
    if entry is None or entry[0] != scope:
        clear_session_read_context(directory)
        entry = (scope, FileReadContext())
        _SESSION_CONTEXTS[key] = entry
    _SESSION_CONTEXTS.move_to_end(key)
    while len(_SESSION_CONTEXTS) > 128:
        _, (_, context) = _SESSION_CONTEXTS.popitem(last=False)
        context.clear()
    return entry[1]


# 恢复或手动压缩会话时使旧上下文及其未交付结果同时失效
def clear_session_read_context(directory: Path) -> None:
    entry = _SESSION_CONTEXTS.pop(path_key(directory), None)
    if entry is not None:
        entry[1].clear()


class FileHistoryStore:
    # 使用会话内内容寻址备份及独立事务记录保存历史
    def __init__(
        self, session_dir: Path, *, max_changes: int = 100, max_bytes: int = 256 * 1024 * 1024
    ) -> None:
        self.directory = session_dir.resolve() / "file-history"
        self.max_changes = max_changes
        self.max_bytes = max_bytes

    # 返回记录目录中的历史，未知或损坏记录不会被静默覆盖
    def records(self) -> list[dict[str, Any]]:
        records = [
            json.loads(path.read_text("utf-8"))
            for path in (self.directory / "changes").glob("*.json")
        ]
        return sorted(records, key=lambda record: (record["timestamp"], record["id"]))

    # 写入或复用内容备份，并验证复用对象的哈希
    def _blob(self, raw: bytes | None) -> str | None:
        digest = version(raw)
        if digest is not None and raw is not None:
            path = self.directory / "blobs" / digest
            if path.exists():
                if version(path.read_bytes()) != digest:
                    raise FileOperationError("File history backup is corrupt", "history_error")
            else:
                atomic_bytes(path, raw, create=True)
        return digest

    # 验证版本并加载备份，缺失备份绝不作为空文件处理
    def read_blob(self, digest: str | None) -> bytes | None:
        if digest is None:
            return None
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise FileOperationError("Invalid backup version", "history_error")
        raw = (self.directory / "blobs" / digest).read_bytes()
        if version(raw) != digest:
            raise FileOperationError("File history backup is corrupt", "history_error")
        return raw

    # 原子保存单条记录，使进程中断不会留下半个 JSON
    def save(self, record: dict[str, Any]) -> None:
        identifier = record["id"]
        uuid.UUID(identifier)
        atomic_bytes(
            self.directory / "changes" / f"{identifier}.json",
            json.dumps(record, ensure_ascii=False).encode("utf-8"),
        )

    # 提交前保存两个版本和待提交记录，任何失败都阻止目标写入
    def prepare(
        self,
        path: Path,
        before: bytes | None,
        after: bytes | None,
        mode: int | None,
        after_mode: int | None,
        operation: str,
        metadata: dict[str, str],
        before_acl: str | None = None,
        after_acl: str | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "path": str(path),
            "operation": operation,
            "timestamp": datetime.now(UTC).isoformat(),
            "status": "pending",
            "before": self._blob(before),
            "after": self._blob(after),
            "before_mode": mode,
            "after_mode": after_mode,
            "before_acl": before_acl,
            "after_acl": after_acl,
            **metadata,
        }
        self.save(record)
        return record

    # 在文件锁下核对中断记录，无法确定的情况保留为明确状态
    def recover(self) -> None:
        with file_lock(self.directory / "index"):
            records = self.records()
        for record in records:
            if record["status"] != "pending":
                continue
            path = Path(record["path"])
            with file_lock(path), file_lock(self.directory / "index"):
                try:
                    fresh = self.get(record["id"])
                except FileNotFoundError:
                    continue
                if fresh["status"] != "pending":
                    continue
                try:
                    if path.resolve() != path or (
                        path.exists()
                        and (not path.is_file() or path.stat().st_size > MAX_FILE_BYTES)
                    ):
                        fresh["status"] = "uncertain"
                        self.save(fresh)
                        continue
                    raw = path.read_bytes() if path.exists() else None
                    digest = version(raw)
                    fresh["status"] = (
                        "committed"
                        if digest == fresh["after"]
                        else "aborted"
                        if digest == fresh["before"]
                        else "uncertain"
                    )
                except OSError:
                    fresh["status"] = "uncertain"
                self.save(fresh)

    # 按标识取得历史，拒绝通过标识进行路径穿越
    def get(self, identifier: str) -> dict[str, Any]:
        uuid.UUID(identifier)
        data: dict[str, Any] = json.loads(
            (self.directory / "changes" / f"{identifier}.json").read_text("utf-8")
        )
        return data

    # 只清理最旧完成记录及无引用内容，保留所有未完成事务
    def prune(self) -> None:
        records = self.records()
        completed = [record for record in records if record["status"] != "pending"]
        while completed:
            referenced = {
                record[key]
                for record in records
                for key in ("before", "after")
                if record[key] is not None
            }
            size = sum((self.directory / "blobs" / digest).stat().st_size for digest in referenced)
            if len(completed) <= self.max_changes and size <= self.max_bytes:
                break
            oldest = completed.pop(0)
            (self.directory / "changes" / f"{oldest['id']}.json").unlink()
            records.remove(oldest)
        referenced = {record[key] for record in records for key in ("before", "after")}
        for blob in (self.directory / "blobs").glob("*"):
            if blob.name not in referenced:
                blob.unlink()


# 核心启动时核对已有会话的未完成提交，损坏历史只记录诊断不阻止守护进程启动
def recover_file_histories(root: Path) -> None:
    for directory in root.glob("*/*/*/*/file-history"):
        try:
            FileHistoryStore(directory.parent).recover()
        except (OSError, ValueError, KeyError):
            logging.getLogger(__name__).exception("Cannot recover file history: %s", directory)


class FileOperationService:
    # 注入会话存储与独立阅读上下文，工具实例共享服务
    def __init__(
        self,
        working_directory: Path | None = None,
        session_dir: Path | None = None,
        context: FileReadContext | None = None,
        agent_id: str = "main",
    ) -> None:
        self.working_directory = working_directory
        self.context = context or FileReadContext()
        self.history = FileHistoryStore(session_dir) if session_dir is not None else None
        self.metadata = {"agent_id": agent_id, "run_id": "", "tool_call_id": ""}
        self.history_warning: str | None = None

    # 统一保持现有路径约束并解析符号链接目标
    def resolve(self, name: str) -> Path:
        path = Path(name)
        if ".." in path.parts:
            raise PermissionError(f"path traversal not allowed: {name}")
        if self.working_directory is not None and not path.is_absolute():
            path = self.working_directory / path
        return path.resolve()

    # 检查修改目标类型、硬链接和文件上限，读取原始字节与权限
    def _current(self, path: Path) -> tuple[bytes | None, int | None]:
        try:
            info = path.stat()
        except FileNotFoundError:
            return None, None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise FileOperationError(
                "Only regular files with one hard link can be modified", "file_validation"
            )
        if info.st_size > MAX_FILE_BYTES:
            raise FileOperationError("File exceeds the 1 MiB modification limit", "file_validation")
        raw = path.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            raise FileOperationError("File exceeds the 1 MiB modification limit", "file_validation")
        return raw, stat.S_IMODE(info.st_mode)

    # 验证当前 Agent 读到的版本及内容覆盖范围
    def _require_read(self, path: Path, raw: bytes, *, full: bool) -> ReadState:
        state = self.context.states.get(path_key(path))
        if state is None:
            raise FileOperationError("Read the file before modifying it", "read_required")
        if state.digest != version(raw):
            raise FileOperationError("File changed since read; read it again before modifying it")
        if full and not state.covers(0, state.total, full=True):
            raise FileOperationError(
                "Read the complete file before overwriting it; previews, "
                "searches and truncated reads are insufficient",
                "read_required",
            )
        return state

    # 在最终版本校验后发布已经 fsync 的临时文件或撤销创建
    def _publish(
        self,
        requested: str,
        path: Path,
        before: bytes | None,
        after: bytes | None,
        mode: int | None,
        acl: str | None = None,
    ) -> None:
        temporary: Path | None = None
        try:
            if after is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(prefix=".agentlite-", dir=path.parent)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(after)
                    stream.flush()
                    os.fsync(stream.fileno())
                if mode is not None:
                    temporary.chmod(mode)
                apply_acl(temporary, acl)
            current, _ = self._current(path)
            if self.resolve(requested) != path or version(current) != version(before):
                raise FileOperationError("File or symlink target changed during commit; read again")
            if after is None:
                path.unlink()
            elif before is None:
                assert temporary is not None
                try:
                    os.link(temporary, path)
                except FileExistsError as exc:
                    raise FileOperationError(
                        "File was created concurrently; read it again"
                    ) from exc
            else:
                assert temporary is not None
                os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    # 保存提交日志后发布文件，发布完成后即使索引确认失败也返回真实提交状态
    def _commit(
        self,
        requested: str,
        path: Path,
        before: bytes | None,
        after: bytes | None,
        mode: int | None,
        after_mode: int | None,
        operation: str,
        acl: str | None = None,
    ) -> str:
        self.history_warning = None
        record = None
        history = self.history
        before_acl = read_acl(path) if before is not None else None
        after_acl = acl if acl is not None else before_acl
        if before == after and mode == after_mode and before_acl == after_acl:
            return "unchanged"
        if history is not None:
            record = history.prepare(
                path,
                before,
                after,
                mode,
                after_mode,
                operation,
                dict(self.metadata),
                before_acl,
                after_acl,
            )
        try:
            self._publish(requested, path, before, after, after_mode, after_acl)
        except BaseException:
            if history is not None and record is not None:
                record["status"] = "aborted"
                history.save(record)
            raise
        if history is not None and record is not None:
            record["status"] = "committed"
            try:
                history.save(record)
                history.prune()
            except (OSError, ValueError):
                self.history_warning = "committed; history confirmation pending"
            return record["id"]  # type: ignore[no-any-return]
        return "committed"

    @contextmanager
    # 串行化文件提交和会话历史索引，固定先文件后历史的锁顺序
    def _locks(self, path: Path) -> Iterator[None]:
        with file_lock(path):
            if self.history is not None:
                with file_lock(self.history.directory / "index"):
                    yield
            else:
                yield

    # 严格覆盖已有 UTF-8 文件，成功后模型已知自己提交的全部新内容
    def write(self, name: str, content: str) -> str:
        path = self.resolve(name)
        if path.suffix.lower() == ".pdf":
            raise FileOperationError(
                "PDF files cannot be written with text tools", "file_validation"
            )
        after = content.encode("utf-8")
        if len(after) > MAX_FILE_BYTES:
            raise FileOperationError("content too large (limit 1 MiB)", "file_validation")
        with self._locks(path):
            before, mode = self._current(path)
            if before is not None:
                self._require_read(path, before, full=True)
                try:
                    before.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise FileOperationError(
                        "Only UTF-8 text can be overwritten", "file_validation"
                    ) from exc
            change = self._commit(
                name, path, before, after, mode, mode if mode is not None else 0o600, "write"
            )
            text = after.decode("utf-8-sig")
            self.context.states[path_key(path)] = ReadState(
                version(after) or "", len(text), [(0, len(text))], [(0, len(text))]
            )
            kind = "create" if before is None else "update"
            warning = f"; {self.history_warning}" if self.history_warning else ""
            return (
                f"{kind}: wrote {len(after)} bytes to {name}; change={change}{warning}\n"
                + text_diff(before, after, name)
            )

    # 精确替换已读片段，保留 BOM 和原始行尾并更新可见区间
    def edit(self, name: str, old: str, new: str, replace_all: bool) -> str:
        if not old:
            raise FileOperationError("old_string must not be empty", "file_validation")
        path = self.resolve(name)
        if path.suffix.lower() == ".pdf":
            raise FileOperationError(
                "PDF files cannot be edited with text tools", "file_validation"
            )
        with self._locks(path):
            before, mode = self._current(path)
            if before is None:
                raise FileOperationError(
                    "File does not exist; use write_file to create it", "file_validation"
                )
            state = self._require_read(path, before, full=False)
            try:
                text = before.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise FileOperationError(
                    "Only UTF-8 text can be edited", "file_validation"
                ) from exc
            # 读取工具返回的行文本不携带 CR，允许按文件行尾规范匹配模型输入
            if "\r\n" in text:
                old = old.replace("\r\n", "\n").replace("\n", "\r\n")
                new = new.replace("\r\n", "\n").replace("\n", "\r\n")
            positions: list[int] = []
            cursor = 0
            while (found := text.find(old, cursor)) >= 0:
                positions.append(found)
                cursor = found + len(old)
            if not positions or (len(positions) > 1 and not replace_all):
                raise FileOperationError(
                    "old_string must match exactly once, or set replace_all for multiple matches",
                    "file_validation",
                )
            if any(not state.covers(pos, pos + len(old)) for pos in positions):
                raise FileOperationError(
                    "Read every replacement target before editing", "read_required"
                )
            updated = text.replace(old, new)
            after = b"\xef\xbb\xbf" if before.startswith(b"\xef\xbb\xbf") else b""
            after += updated.encode("utf-8")
            if len(after) > MAX_FILE_BYTES:
                raise FileOperationError("Edited content exceeds 1 MiB", "file_validation")
            change = self._commit(name, path, before, after, mode, mode, "edit")
            delta = len(new) - len(old)

            # 按替换位置移动可见区间边界，不把未读区域提升为已读
            def move(boundary: int) -> int:
                return boundary + sum(delta for pos in positions if pos + len(old) <= boundary)

            self.context.states[path_key(path)] = ReadState(
                version(after) or "",
                len(updated),
                [(move(a), move(b)) for a, b in state.spans],
                [(move(a), move(b)) for a, b in state.full_spans],
            )
            return (
                f"edited {len(positions)} occurrence(s) in {name}; change={change}"
                + (f"; {self.history_warning}" if self.history_warning else "")
                + "\n"
                + text_diff(before, after, name)
            )

    # 在相同提交协议下恢复一次修改，不允许覆盖后续或外部变化
    def restore(self, identifier: str, *, dry_run: bool = False) -> dict[str, Any]:
        history = self.history
        if history is None:
            raise FileOperationError("File history is unavailable", "history_error")
        record = history.get(identifier)
        path = Path(record["path"])
        with self._locks(path):
            record = history.get(identifier)
            if record["status"] != "committed":
                raise FileOperationError("Only committed changes can be restored", "history_error")
            current, mode = self._current(path)
            if version(current) != record["after"]:
                raise FileOperationError(
                    "Restore conflict: undo newer changes first; "
                    "external modifications are never overwritten"
                )
            target = history.read_blob(record["before"])
            result: dict[str, Any] = {
                "path": str(path),
                "dry_run": dry_run,
                "diff": text_diff(current, target, str(path)),
            }
            if not dry_run:
                self.metadata["restores"] = identifier
                try:
                    change = self._commit(
                        str(path),
                        path,
                        current,
                        target,
                        mode,
                        record["before_mode"],
                        "restore",
                        record.get("before_acl"),
                    )
                finally:
                    self.metadata.pop("restores", None)
                result["change_id"] = None if change == "unchanged" else change
                if self.history_warning:
                    result["warning"] = self.history_warning
                self.context.states.pop(path_key(path), None)
            return result
