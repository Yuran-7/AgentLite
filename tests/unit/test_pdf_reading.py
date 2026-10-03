from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from agent_lite.core.compact.compactor import _messages_to_text
from agent_lite.core.context import ExecutionContext
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.assets import expand_assets
from agent_lite.core.llm.openai_provider import _convert_messages
from agent_lite.core.session.ids import new_session_id
from agent_lite.core.session.store import SessionStore
from agent_lite.core.tools.builtin.read_file import ReadFileParams, ReadFileTool
from agent_lite.core.tools.file_operations import FileOperationError, FileOperationService
from agent_lite.core.tools.pdf_worker import page_range


# 生成自包含文本与矢量图形 PDF，避免依赖外部下载或额外生成库
def make_pdf(path: Path, count: int = 1, *, text: bool = True) -> None:
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids = []
    for index in range(count):
        page_id, stream_id = len(objects) + 1, len(objects) + 2
        kids.append(f"{page_id} 0 R")
        commands = b"0.2 0.4 0.8 rg 20 20 100 40 re f\n"
        if text:
            commands += f"BT /F1 20 Tf 40 100 Td (Page {index + 1} hello) Tj ET\n".encode()
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {stream_id} 0 R >>"
            ).encode()
        )
        objects.append(
            f"<< /Length {len(commands)} >>\nstream\n".encode() + commands + b"endstream"
        )
    objects[1] = f"<< /Type /Pages /Count {count} /Kids [{' '.join(kids)}] >>".encode()
    raw = b"%PDF-1.4\n"
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(raw)
    raw += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    raw += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    raw += (f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
    path.write_bytes(raw)


# 功能：验证真实 PDF 工作进程提取文字并生成带页码的图片资产
# 设计：使用自制文本和图形 PDF，检查缓存复用、图片尺寸及编码预算
async def test_pdf_text_images_and_asset_cache(tmp_path: Path) -> None:
    path = tmp_path / "paper.pdf"
    make_pdf(path, 2)
    service = FileOperationService(tmp_path, tmp_path / "session")
    tool = ReadFileTool(service=service)
    result = await tool.invoke({"path": "paper.pdf"})
    assert "Page 1 hello" in result.content and "Page 2 hello" in result.content
    images = [block for block in result.blocks if block["type"] == "image"]
    assert len(images) == 2
    image_path = Path(images[0]["source"]["path"])
    info = image_path.stat()
    with Image.open(image_path) as image:
        assert max(image.size) <= 1600
    assert len(base64.b64encode(image_path.read_bytes())) <= 1024 * 1024
    second = await tool.invoke({"path": "paper.pdf", "pages": "1", "pdf_mode": "images"})
    assert second.blocks[-1]["source"]["path"] == str(image_path)
    assert image_path.stat().st_mtime_ns == info.st_mtime_ns
    result.confirm_delivery()
    assert not service.context.states
    make_pdf(path, 1)
    third = await tool.invoke({"path": "paper.pdf", "pages": "1"})
    assert third.blocks[-1]["source"]["path"] != str(image_path)


# 功能：验证长文档必须分页，指定单页只返回该页且文本模式不生成图片
# 设计：六页文档触发默认阈值并验证尾页范围，不依赖工具内部实现
async def test_pdf_long_document_requires_pages(tmp_path: Path) -> None:
    path = tmp_path / "long.pdf"
    make_pdf(path, 6)
    tool = ReadFileTool(service=FileOperationService(tmp_path, tmp_path / "session"))
    with pytest.raises(FileOperationError, match="6 pages; specify"):
        await tool.invoke({"path": "long.pdf"})
    result = await tool.invoke({"path": "long.pdf", "pages": "6", "pdf_mode": "text"})
    assert "Page 6 hello" in result.content and "Page 1 hello" not in result.content
    assert not result.blocks
    assert not list((tmp_path / "session").rglob("*.jpg"))


# 功能：验证扫描内容明确提示无文字，损坏文件返回 PDF 专用错误
# 设计：构造纯图形页面与非法字节，确保不把二进制正文发给模型
async def test_pdf_scanned_page_and_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    make_pdf(path, text=False)
    tool = ReadFileTool(service=FileOperationService(tmp_path, tmp_path / "session"))
    result = await tool.invoke({"path": "scan.pdf", "pdf_mode": "text"})
    assert "No extractable text" in result.content
    path.write_bytes(b"not a PDF")
    with pytest.raises(FileOperationError, match="encrypted or damaged"):
        await tool.invoke({"path": "scan.pdf"})


# 功能：验证非法页范围、超过五页和文本参数混用被拒绝
# 设计：参数化边界输入，分别检查工具 schema 与 PDF 页范围解析
@pytest.mark.parametrize("pages", ["0", "4-2", "1-6", "100", "1,3", "all", ""])
def test_pdf_page_ranges_rejected(pages: str) -> None:
    with pytest.raises(ValueError):
        page_range(pages, 10)
    with pytest.raises(ValidationError):
        ReadFileParams(path="file.pdf", pages="1", end_line=4)


# 功能：验证 PDF 页面取消时终止并回收工作进程
# 设计：使用阻塞假进程精确观察 kill 和 wait，而无需依赖慢 PDF
async def test_pdf_cancellation_kills_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_lite.core.tools.pdf_reader import read_pdf

    path = tmp_path / "paper.pdf"
    make_pdf(path)
    started = asyncio.Event()

    class Process:
        returncode: int | None = None
        killed = False
        waited = False

        # 模拟正在等待 PDF 解析结果的进程
        async def communicate(self, _request: bytes) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Event().wait()
            return b"", b""

        # 记录进程终止动作
        def kill(self) -> None:
            self.killed = True
            self.returncode = -1

        # 记录回收动作，防止取消后留下孤儿进程
        async def wait(self) -> int:
            self.waited = True
            return -1

    process = Process()

    # 替换进程启动，避免测试时启动真正的长任务
    async def create(*_args: object, **_kwargs: object) -> Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(read_pdf(path, None, "both", tmp_path / "session"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.waited


# 功能：验证图片引用贯通持久化、压缩和两个 provider 的请求转换
# 设计：同一步两个工具响应带图片，检查所有 tool 消息先于图片且原历史无 base64
async def test_asset_messages_persistence_and_provider_conversion(tmp_path: Path) -> None:
    path = tmp_path / "paper.pdf"
    make_pdf(path)
    session_id = new_session_id()
    store = SessionStore(tmp_path / "sessions")
    directory = store.session_dir(session_id)
    tool = ReadFileTool(service=FileOperationService(tmp_path, directory))
    result = await tool.invoke({"path": "paper.pdf"})
    context = ExecutionContext("run", "read PDF", 3)
    context.add_assistant_message(
        [
            {"type": "tool_use", "id": "pdf", "name": "read_file", "input": {"path": str(path)}},
            {"type": "tool_use", "id": "other", "name": "read_file", "input": {"path": str(path)}},
        ]
    )
    context.add_tool_result("pdf", result.model_content())
    context.add_tool_result("other", "other output")
    store.append_messages(session_id, context.messages, "run")
    persisted = (directory / "thread.jsonl").read_text("utf-8")
    assert '"type": "file"' in persisted and '"type": "base64"' not in persisted
    messages = store.read_messages(session_id)
    expanded = expand_assets(messages)
    image = expanded[-1]["content"][0]["content"][-1]
    assert image["source"]["type"] == "base64"
    raw = base64.b64decode(image["source"]["data"])
    with Image.open(io.BytesIO(raw)) as loaded:
        assert loaded.format == "JPEG"
    converted = _convert_messages(messages, None)
    assert [item["role"] for item in converted[-3:]] == ["tool", "tool", "user"]
    parts: Any = converted[-1]["content"]
    assert "Tool result pdf:" in parts[0]["text"] and "page 1" in parts[0]["text"]
    assert parts[1]["type"] == "image_url"
    summary = _messages_to_text(messages)
    assert "图片资产" in summary and "base64" not in summary
    assert '"type": "base64"' not in (directory / "thread.jsonl").read_text("utf-8")


# 功能：验证 Anthropic 请求展开嵌套工具图片而保留原始历史的文件引用
# 设计：使用已有流式客户端替身捕获真实 provider 入参，覆盖发送前的最后转换
async def test_anthropic_expands_tool_images_at_api_boundary(tmp_path: Path) -> None:
    from tests.unit.test_llm_provider import _make_provider
    path = tmp_path / "image.jpg"
    Image.new("RGB", (2, 2), "red").save(path)
    messages = [{"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "pdf", "content": [{
            "type": "image", "source": {
                "type": "file", "path": str(path), "media_type": "image/jpeg",
            },
        }],
    }]}]
    provider, client = _make_provider()
    await provider.chat(messages, [], EventBus(), "run")
    sent = client.messages.stream.call_args.kwargs["messages"]
    assert sent[0]["content"][0]["content"][0]["source"]["type"] == "base64"
    assert messages[0]["content"][0]["content"][0]["source"]["type"] == "file"
