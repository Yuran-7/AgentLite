from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_lite.core.context import ExecutionContext
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.types import LlmResponse, ToolCallBlock
from agent_lite.core.loop import AgentLoop
from agent_lite.core.tools.builtin.edit_file import EditFileTool
from agent_lite.core.tools.builtin.read_file import ReadFileTool
from agent_lite.core.tools.builtin.write_file import WriteFileTool
from agent_lite.core.tools.file_operations import (
    FileOperationError,
    FileOperationService,
    clear_session_read_context,
    read_acl,
    session_read_context,
)
from agent_lite.core.tools.invocation import invoke_tool
from agent_lite.core.tools.registry import ToolRegistry
from agent_lite.core.tools.result_storage import ToolResultStore


# 读取后显式模拟上下文交付，避免测试绕过真实阅读资格建立时机
async def read(service: FileOperationService, **params: object) -> str:
    result = await ReadFileTool(service=service).invoke({"path": "file.txt", **params})
    result.confirm_delivery()
    return result.content


# 功能：验证交付前没有资格，交付后去重及强制重读可用
# 设计：分开调用读取和交付，排除工具读取磁盘即授权覆盖的错误实现
async def test_delivery_dedup_force_and_clear(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_bytes(b"original")
    service = FileOperationService(tmp_path, tmp_path / "session")
    tool = ReadFileTool(service=service)
    result = await tool.invoke({"path": "file.txt"})
    with pytest.raises(FileOperationError, match="Read the file"):
        service.write("file.txt", "new")
    result.confirm_delivery()
    assert "file_unchanged" in await read(service)
    assert await read(service, force=True) == "original"
    service.context.clear()
    assert await read(service) == "original"


# 功能：验证哈希检测同时间戳改动，忽略只有时间戳的变化
# 设计：手动保留原始 mtime 后改写内容，避免元数据捷径掩盖冲突
async def test_hash_version_over_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"first")
    service = FileOperationService(tmp_path)
    await read(service)
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 100_000_000))
    assert "file_unchanged" in await read(service)
    service.write("file.txt", "second")
    now = path.stat()
    path.write_bytes(b"third!")
    os.utime(path, ns=(now.st_atime_ns, now.st_mtime_ns))
    with pytest.raises(FileOperationError, match="changed since read"):
        service.write("file.txt", "overwrite")
    assert await read(service) == "third!"


# 功能：验证多个已读行区间能累计完整覆盖，搜索不能授权整文件覆盖
# 设计：按两段读取同版本文件，另一个隔离上下文只搜索全部行
async def test_ranges_accumulate_search_does_not_authorize(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_bytes(b"one\ntwo\nthree\n")
    service = FileOperationService(tmp_path)
    await read(service, end_line=1)
    with pytest.raises(FileOperationError, match="complete file"):
        service.write("file.txt", "oops")
    await read(service, start_line=2, end_line=3)
    service.write("file.txt", "all\n")
    other = FileOperationService(tmp_path)
    await read(other, search="all")
    with pytest.raises(FileOperationError, match="complete file"):
        other.write("file.txt", "oops")


# 功能：验证首尾预览无覆盖资格，重复读取仍保留全文落盘位置
# 设计：经真实调用器压缩大结果，再显式交付，覆盖两层截断的集成边界
async def test_preview_does_not_authorize_and_dedup_keeps_location(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_bytes(b"some line\n" * 1000)
    service = FileOperationService(tmp_path)
    registry = ToolRegistry()
    registry.register(ReadFileTool(service=service))
    result = await invoke_tool(
        registry,
        ToolCallBlock("read-1", "read_file", {"path": "file.txt"}),
        EventBus(),
        "run",
        result_store=ToolResultStore(tmp_path / "session", limit_chars=100),
    )
    result.confirm_delivery()
    assert result.truncated and result.output_path
    with pytest.raises(FileOperationError, match="complete file"):
        service.write("file.txt", "oops")
    repeated = await read(service)
    assert result.output_path in repeated
    assert "file_unchanged" in repeated


# 功能：验证局部编辑只修改实际已读目标，replace_all 不能扩展到未读匹配
# 设计：相同文本分布在不同行，分阶段授权全部匹配
async def test_edit_visible_targets_and_all_matches(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"same\nsecret\nsame\n")
    service = FileOperationService(tmp_path)
    await read(service, end_line=1)
    with pytest.raises(FileOperationError, match="exactly once"):
        service.edit("file.txt", "same", "new", False)
    with pytest.raises(FileOperationError, match="every replacement"):
        service.edit("file.txt", "same", "new", True)
    with pytest.raises(FileOperationError, match="every replacement"):
        service.edit("file.txt", "secret", "new", False)
    await read(service, start_line=3, end_line=3)
    service.edit("file.txt", "same", "longer", True)
    assert path.read_bytes() == b"longer\nsecret\nlonger\n"
    service.edit("file.txt", "longer", "done", True)
    with pytest.raises(FileOperationError, match="complete file"):
        service.write("file.txt", "not authorized")


# 功能：验证跨行替换保留 BOM、CRLF、权限与后续完整覆盖资格
# 设计：实际原始字节断言避开文本模式自动换行掩盖错误
async def test_edit_preserves_bom_crlf_and_coverage(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\nthree\r\n")
    original_mode = path.stat().st_mode
    service = FileOperationService(tmp_path)
    await read(service, end_line=1)
    await read(service, start_line=2, end_line=3)
    service.edit("file.txt", "one\ntwo", "new", False)
    assert path.read_bytes() == b"\xef\xbb\xbfnew\r\nthree\r\n"
    assert path.stat().st_mode == original_mode
    service.write("file.txt", "explicit\r\nLF\n")
    assert path.read_bytes() == b"explicit\r\nLF\n"


# 功能：验证空文件搜索仍不能授权覆盖，真正读取空文件可以
# 设计：覆盖零长度区间容易被默认判为完整的特殊边界
async def test_empty_search_is_not_full_read(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_bytes(b"")
    service = FileOperationService(tmp_path)
    await read(service, search="nothing")
    with pytest.raises(FileOperationError, match="complete file"):
        service.write("file.txt", "oops")
    await read(service)
    service.write("file.txt", "valid")


# 功能：验证同会话跨轮次复用，恢复与上下文切换清空旧资格
# 设计：直接检查缓存对象身份及失效代数，覆盖未交付读取被清除的情况
def test_session_context_lifecycle(tmp_path: Path) -> None:
    first = session_read_context(tmp_path, "scope-a")
    assert session_read_context(tmp_path, "scope-a") is first
    second = session_read_context(tmp_path, "scope-b")
    assert second is not first and first.epoch == 1
    clear_session_read_context(tmp_path)
    assert second.epoch == 1
    assert session_read_context(tmp_path, "scope-b") is not second


# 功能：验证并发 Agent 各自读同版本后只有一次修改成功
# 设计：并发提交到同一历史目录，检查冲突及成功记录数量
async def test_concurrent_agents_have_isolated_reads(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_bytes(b"before")
    services = [
        FileOperationService(tmp_path, tmp_path / "session", agent_id=str(i)) for i in range(2)
    ]
    for service in services:
        await read(service)
    results = await asyncio.gather(
        *(
            WriteFileTool(service=service).invoke({"path": "file.txt", "content": f"new-{i}"})
            for i, service in enumerate(services)
        )
    )
    assert sum(not result.is_error for result in results) == 1
    assert [r.error_type for r in results if r.is_error] == ["file_conflict"]
    history = services[0].history
    assert history is not None and len(history.records()) == 1
    third = FileOperationService(tmp_path)
    assert (
        await WriteFileTool(service=third).invoke({"path": "file.txt", "content": "unread"})
    ).error_type == "read_required"


# 功能：验证备份失败或原子发布失败时旧文件保持完整且临时文件清理
# 设计：分别在备份和 replace 注入 I/O 失败，检查磁盘内容及记录状态
@pytest.mark.parametrize("failure", ["backup", "replace"])
async def test_failures_preserve_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"before")
    service = FileOperationService(tmp_path, tmp_path / "session")
    await read(service)
    assert service.history is not None
    if failure == "backup":
        # 模拟历史存储不可用，不允许跳过备份继续写目标
        def fail_blob(_raw: bytes | None) -> None:
            raise OSError("backup unavailable")

        monkeypatch.setattr(service.history, "_blob", fail_blob)
    else:
        original = os.replace

        # 仅拒绝目标发布，允许历史记录的原子保存继续工作
        def fail_replace(src: object, dst: object) -> None:
            if Path(str(dst)) == path:
                raise OSError("disk full")
            original(src, dst)

        monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        service.write("file.txt", "after")
    assert path.read_bytes() == b"before"
    assert not list(tmp_path.glob(".agentlite-*"))
    if failure == "replace":
        assert service.history.records()[0]["status"] == "aborted"


# 功能：验证取消已开始提交会等待真实结果且不会留下后台写入
# 设计：用线程事件精确阻塞发布，取消协程后再释放，排除依赖时序的偶然通过
async def test_cancelled_commit_finishes_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = FileOperationService(tmp_path, tmp_path / "session")
    entered, release = threading.Event(), threading.Event()
    original = service._publish

    # 确认进入提交后阻塞，外层取消不应提前结束返回
    def blocked(*args: object) -> None:
        entered.set()
        assert release.wait(5)
        original(*args)

    monkeypatch.setattr(service, "_publish", blocked)
    task = asyncio.create_task(
        WriteFileTool(service=service).invoke({"path": "file.txt", "content": "complete"})
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    result = await task
    assert not result.is_error and (tmp_path / "file.txt").read_bytes() == b"complete"
    assert service.history is not None
    assert service.history.records()[0]["status"] == "committed"


# 功能：验证历史内容去重、逆序恢复、创建撤销及恢复自身可撤销
# 设计：同一文件多次编辑并恢复，使用真实备份核对每一步原始内容
async def test_history_restore_and_deduplicated_blobs(tmp_path: Path) -> None:
    service = FileOperationService(tmp_path, tmp_path / "session")
    service.write("file.txt", "one")
    service.write("file.txt", "two")
    history = service.history
    assert history is not None
    create, update = history.records()
    assert len(list((history.directory / "blobs").iterdir())) == 2
    with pytest.raises(FileOperationError, match="undo newer"):
        service.restore(create["id"])
    preview = service.restore(update["id"], dry_run=True)
    assert preview["dry_run"] and (tmp_path / "file.txt").read_bytes() == b"two"
    assert len(history.records()) == 2
    restored = service.restore(update["id"])
    assert (tmp_path / "file.txt").read_bytes() == b"one"
    service.restore(create["id"])
    assert not (tmp_path / "file.txt").exists()
    removal = history.records()[-1]
    service.restore(removal["id"])
    assert (tmp_path / "file.txt").read_bytes() == b"one"
    service.restore(restored["change_id"])
    assert (tmp_path / "file.txt").read_bytes() == b"two"
    (tmp_path / "file.txt").write_bytes(b"external")
    with pytest.raises(FileOperationError, match="external"):
        service.restore(update["id"])


# 功能：验证 pending 记录根据磁盘版本恢复为成功、未提交或不确定
# 设计：直接保留待提交记录，模拟进程在不同提交时刻中断
@pytest.mark.parametrize(
    "raw,status", [(b"after", "committed"), (b"before", "aborted"), (b"other", "uncertain")]
)
def test_pending_recovery(tmp_path: Path, raw: bytes, status: str) -> None:
    path = tmp_path / "file.txt"
    service = FileOperationService(tmp_path, tmp_path / "session")
    history = service.history
    assert history is not None
    record = history.prepare(path, b"before", b"after", 0o600, 0o600, "write", {})
    path.write_bytes(raw)
    history.recover()
    assert history.get(record["id"])["status"] == status


# 功能：验证历史清理满足记录数量与字节预算，并保留未完成事务的备份
# 设计：构造连续版本和 pending 记录，检查引用对象未被错误删除
def test_history_prune_protects_pending(tmp_path: Path) -> None:
    service = FileOperationService(tmp_path, tmp_path / "session")
    history = service.history
    assert history is not None
    history.max_changes = 2
    history.max_bytes = 20
    for content in ("1111", "2222", "3333", "4444"):
        service.write("file.txt", content)
    pending = history.prepare(
        tmp_path / "pending.txt", b"pending", b"next", None, None, "write", {}
    )
    with service._locks(tmp_path / "file.txt"):
        history.prune()
    assert len([r for r in history.records() if r["status"] == "committed"]) <= 2
    assert history.read_blob(pending["before"]) == b"pending"
    assert history.read_blob(pending["after"]) == b"next"


# 功能：验证硬链接、二进制编码、PDF 和大小限制不能被文本修改绕过
# 设计：组合实际文件类型与非法内容，保证失败时目标仍存在且不被改写
async def test_validation_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"\xff")
    service = FileOperationService(tmp_path)
    await read(service)
    with pytest.raises(FileOperationError, match="UTF-8"):
        service.edit("file.txt", "x", "y", False)
    with pytest.raises(FileOperationError, match="1 MiB"):
        service.write("big.txt", "x" * (1024 * 1024 + 1))
    with pytest.raises(FileOperationError, match="PDF"):
        service.write("document.pdf", "not a PDF")
    os.link(path, tmp_path / "hard.txt")
    with pytest.raises(FileOperationError, match="hard link"):
        service.write("file.txt", "overwrite")


# 功能：验证新增编辑工具使用真实调用器时返回不可重试的明确错误
# 设计：不预先读取并收集失败事件，确认只尝试一次且未修改文件
async def test_read_required_errors_are_not_retried(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_bytes(b"old")
    service = FileOperationService(tmp_path)
    registry = ToolRegistry()
    registry.register(EditFileTool(service=service))
    events: list[object] = []
    bus = EventBus()

    # 收集工具事件以检查错误尝试次数
    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    result = await invoke_tool(
        registry,
        ToolCallBlock(
            "e",
            "edit_file",
            {
                "path": "file.txt",
                "old_string": "old",
                "new_string": "new",
            },
        ),
        bus,
        "run",
    )
    assert result.error_type == "read_required"
    failures = [e for e in events if getattr(e, "type", "") == "tool.call_failed"]
    assert len(failures) == 1


# 功能：验证真实进程共享文件锁，竞争同版本文件只提交一次
# 设计：两个独立解释器先读再通过 stdin 屏障同时写入，排除仅线程锁生效的假通过
async def test_cross_process_conflict(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_bytes(b"before")
    script = """
import asyncio, json, sys
from pathlib import Path
from agent_lite.core.tools.file_operations import FileOperationService, FileOperationError
from agent_lite.core.tools.builtin.read_file import ReadFileTool
async def main():
    service = FileOperationService(Path(sys.argv[1]), Path(sys.argv[1]) / 'session')
    result = await ReadFileTool(service=service).invoke({'path': 'file.txt'})
    result.confirm_delivery()
    print('ready', flush=True)
    sys.stdin.readline()
    try:
        service.write('file.txt', sys.argv[2])
        print(json.dumps({'status': 'committed'}))
    except FileOperationError as exc:
        print(json.dumps({'status': exc.error_type}))
asyncio.run(main())
"""
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            f"after-{i}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for i in range(2)
    ]
    try:
        for process in processes:
            assert process.stdout is not None
            assert (await asyncio.wait_for(process.stdout.readline(), 15)).strip() == b"ready"
        outputs = await asyncio.wait_for(
            asyncio.gather(*(process.communicate(b"go\n") for process in processes)), 15
        )
        statuses = [json.loads(stdout)["status"] for stdout, _stderr in outputs]
        assert sorted(statuses) == ["committed", "file_conflict"]
        history = FileOperationService(tmp_path, tmp_path / "session").history
        assert history is not None and len(history.records()) == 1
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()


# 功能：验证最终校验发现备份之后的外部修改或并发创建
# 设计：在第二次读取目标前写入外部版本，确保提交不覆盖且 pending 转为 aborted
@pytest.mark.parametrize("existing", [True, False])
async def test_final_validation_preserves_concurrent_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    path = tmp_path / "file.txt"
    service = FileOperationService(tmp_path, tmp_path / "session")
    if existing:
        path.write_bytes(b"before")
        await read(service)
    original = service._current
    calls = 0

    # 在待提交记录之后、原子发布之前模拟外部写入
    def current(target: Path) -> tuple[bytes | None, int | None]:
        nonlocal calls
        calls += 1
        if calls == 2:
            target.write_bytes(b"external")
        return original(target)

    monkeypatch.setattr(service, "_current", current)
    with pytest.raises(FileOperationError, match="during commit"):
        service.write("file.txt", "agent")
    assert path.read_bytes() == b"external"
    assert service.history is not None
    assert service.history.records()[0]["status"] == "aborted"


# 功能：验证提交成功但索引确认失败时不报告写失败且能在重启时核对
# 设计：只拒绝 committed 记录保存，保留真实 pending 记录和已发布文件
def test_history_confirmation_failure_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = FileOperationService(tmp_path, tmp_path / "session")
    history = service.history
    assert history is not None
    original = history.save

    # 仅注入发布完成后的确认失败，备份阶段仍正常
    def save(record: dict) -> None:
        if record["status"] == "committed":
            raise OSError("confirmation failed")
        original(record)

    with monkeypatch.context() as patch:
        patch.setattr(history, "save", save)
        result = service.write("file.txt", "complete")
    assert "committed" in result and "confirmation pending" in result
    assert (tmp_path / "file.txt").read_bytes() == b"complete"
    assert history.records()[0]["status"] == "pending"
    history.recover()
    assert history.records()[0]["status"] == "committed"
    service.write("file.txt", "second")
    change = history.records()[-1]
    with monkeypatch.context() as patch:
        patch.setattr(history, "save", save)
        restored = service.restore(change["id"])
    assert restored["warning"] == "committed; history confirmation pending"
    assert history.get(restored["change_id"])["status"] == "pending"
    assert (tmp_path / "file.txt").read_bytes() == b"complete"
    history.recover()
    assert history.get(restored["change_id"])["status"] == "committed"


# 功能：验证无需变化的修改不产生历史，备份损坏时拒绝恢复
# 设计：对真实内容对象注入损坏并核对目标版本保持不变
def test_noop_and_corrupt_backup(tmp_path: Path) -> None:
    service = FileOperationService(tmp_path, tmp_path / "session")
    service.write("file.txt", "first")
    service.write("file.txt", "first")
    service.edit("file.txt", "first", "first", False)
    history = service.history
    assert history is not None and len(history.records()) == 1
    service.write("file.txt", "second")
    record = history.records()[-1]
    (history.directory / "blobs" / record["before"]).write_bytes(b"corrupt")
    with pytest.raises(FileOperationError, match="corrupt"):
        service.restore(record["id"])
    assert (tmp_path / "file.txt").read_bytes() == b"second"


# 功能：验证被搜索匹配上限截断的结果只授权已返回目标的局部编辑
# 设计：第一条匹配实际交付，第二条匹配省略，分开验证两者权限
async def test_search_limit_keeps_visible_target_qualification(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_bytes(b"match-one\nmatch-two\n")
    service = FileOperationService(tmp_path)
    await read(service, search="match", max_matches=1)
    service.edit("file.txt", "match-one", "changed", False)
    with pytest.raises(FileOperationError, match="every replacement"):
        service.edit("file.txt", "match-two", "changed", False)
    with pytest.raises(FileOperationError, match="complete file"):
        service.write("file.txt", "overwrite")


# 功能：验证回放被裁成预览时清空跨轮次资格，正常追加消息则保留
# 设计：使用同一正文的前缀与缩短版本，不依赖具体会话迁移实现
async def test_replay_integrity_and_late_delivery(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_bytes(b"before")
    service = FileOperationService(tmp_path)
    await read(service)
    context = service.context
    context.remember_messages([{"role": "user", "content": "full content"}])
    context.verify_replay(
        [{"role": "user", "content": "full content"}, {"role": "user", "content": "next turn"}]
    )
    assert context.states
    context.verify_replay([{"role": "user", "content": "preview"}])
    assert not context.states
    pending = await ReadFileTool(service=service).invoke({"path": "file.txt"})
    context.clear()
    pending.confirm_delivery()
    assert not context.states


# 功能：验证 Windows 原子更新和历史恢复保留原文件 DACL
# 设计：真实读取安全描述符并比较两次发布后的权限，避免只验证 POSIX mode
@pytest.mark.skipif(os.name != "nt", reason="Windows DACL only")
async def test_windows_acl_preserved(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"before")
    acl = read_acl(path)
    service = FileOperationService(tmp_path, tmp_path / "session")
    await read(service)
    service.write("file.txt", "after")
    assert read_acl(path) == acl
    assert service.history is not None
    change = service.history.records()[0]
    assert change["before_acl"] == acl
    service.restore(change["id"])
    assert read_acl(path) == acl


# 功能：验证同批预生成的读写调用不能伪装为模型已看过文件
# 设计：一次响应同时返回 read 和 write，下一模型轮次后才授权同一阅读结果
async def test_same_batch_read_does_not_authorize_preplanned_write(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"before")
    service = FileOperationService(tmp_path)
    registry = ToolRegistry()
    registry.register(ReadFileTool(service=service))
    registry.register(WriteFileTool(service=service))
    provider = MagicMock()
    provider.chat = AsyncMock(
        side_effect=[
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    ToolCallBlock("read", "read_file", {"path": "file.txt"}),
                    ToolCallBlock("write", "write_file", {"path": "file.txt", "content": "unsafe"}),
                ],
            ),
            LlmResponse(stop_reason="end_turn", text="done"),
        ]
    )
    context = ExecutionContext("run", "goal", 3)
    await AgentLoop(provider, registry, EventBus()).run(context)
    assert path.read_bytes() == b"before"
    results = context.messages[2]["content"]
    assert results[1]["is_error"] and "Read the file" in results[1]["content"]
    service.write("file.txt", "safe after model delivery")


# 功能：验证真实 Runner 跨轮次保留阅读资格及修改历史调用标识
# 设计：两轮使用不同 Runner 实例，分别读取与写入，覆盖实际会话工厂生命周期
async def test_runner_preserves_read_context_across_turns(tmp_path: Path) -> None:
    from agent_lite.core.config import AgentLiteConfig
    from agent_lite.core.runner import AgentRunner
    from agent_lite.core.session.ids import new_session_id
    from agent_lite.core.session.model import Session
    from agent_lite.core.session.store import SessionStore

    path = tmp_path / "file.txt"
    path.write_bytes(b"before")
    store = SessionStore(tmp_path / "sessions")
    session = Session(
        new_session_id(), "chat", "active", "test", "t", "t", workspace_root=str(tmp_path)
    )
    store.write_meta(session)
    store.append_message(session.id, "user", "read")
    for index, name in enumerate(("read_file", "write_file")):
        provider = MagicMock()
        params: dict[str, object] = {"path": "file.txt"}
        if name == "write_file":
            params["content"] = "after"
            store.append_message(session.id, "user", "write")
        provider.chat = AsyncMock(
            side_effect=[
                LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[ToolCallBlock(f"call-{index}", name, params)],
                ),
                LlmResponse(stop_reason="end_turn", text="done"),
            ]
        )
        runner = AgentRunner(AgentLiteConfig(), provider=provider)
        outcome = await runner.run_and_capture(
            name, run_id=f"run-{index}", session=session, store=store
        )
        assert outcome.status == "success"
    assert path.read_bytes() == b"after"
    history = FileOperationService(tmp_path, store.session_dir(session.id)).history
    assert history is not None
    record = history.records()[0]
    assert record["run_id"] == "run-1" and record["tool_call_id"] == "call-1"
