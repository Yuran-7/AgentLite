from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from agent_lite.core.trace.record import TraceRecord

logger = logging.getLogger(__name__)


class TraceWriter:
    # 初始化 TraceWriter；写入目标文件路径在 start() 前不会创建
    def __init__(self, path: Path) -> None:
        self._path = path
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    # 创建目录、启动后台 drain task
    async def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_legacy_encoding()
        self._task = asyncio.create_task(self._drain())

    # 等待队列清空后取消 drain task
    async def stop(self) -> None:
        task = self._task
        if task is None:
            self._discard_pending()
            return

        join_task = asyncio.create_task(self._queue.join())
        try:
            done, _pending = await asyncio.wait(
                {join_task, task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                self._report_task_failure(task)
                self._discard_pending()
                await join_task
        finally:
            if not join_task.done():
                join_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await join_task
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            self._task = None

    # 非阻塞地将 record 放入写入队列
    def emit(self, record: TraceRecord) -> None:
        if self._task is not None and self._task.done():
            self._report_task_failure(self._task)
            return
        self._queue.put_nowait(record)

    # 持续从队列读取 record 并追加写入文件
    async def _drain(self) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            while True:
                record = await self._queue.get()
                try:
                    f.write(record.model_dump_json() + "\n")
                    f.flush()
                except (OSError, UnicodeError, ValueError):
                    logger.exception("failed to write trace record path=%s", self._path)
                finally:
                    self._queue.task_done()

    # 丢弃后台任务退出后无人消费的记录，并同步 queue.join 的 unfinished 计数
    def _discard_pending(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._queue.task_done()

    # 将旧版非 UTF-8 trace 无损移到备份文件，避免后续追加形成混合编码
    def _rotate_legacy_encoding(self) -> None:
        if not self._path.exists() or not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as file:
                while file.read(1024 * 1024):
                    pass
            return
        except UnicodeDecodeError:
            pass
        backup = self._path.with_name(f"{self._path.name}.legacy-encoding.bak")
        suffix = 1
        while backup.exists():
            backup = self._path.with_name(
                f"{self._path.name}.legacy-encoding.{suffix}.bak"
            )
            suffix += 1
        self._path.replace(backup)
        logger.warning("rotated non-UTF-8 trace file to %s", backup)

    # 读取并记录后台任务异常，避免 asyncio 在事件循环退出时再次报告未回收异常
    @staticmethod
    def _report_task_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("trace writer task stopped unexpectedly: %s", error)
