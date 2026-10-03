from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from agent_lite.core.tools.base import ToolResult
from agent_lite.core.tools.file_operations import (
    FileOperationError,
    atomic_bytes,
    file_lock,
    version,
)

MAX_PDF_BYTES = 64 * 1024 * 1024


# 固定 PDF 字节版本，避免渲染过程中原文件被外部替换
def _snapshot(path: Path, session_dir: Path | None) -> tuple[Path, Path]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_PDF_BYTES + 1)
    if len(raw) > MAX_PDF_BYTES:
        raise FileOperationError("PDF exceeds the 64 MiB read limit", "pdf_error")
    root = session_dir or Path(tempfile.gettempdir()) / "agentlite-pdf"
    directory = root.resolve() / "assets" / "pdf" / (version(raw) or "")
    source = directory / "source.pdf"
    with file_lock(source):
        if not source.exists():
            atomic_bytes(source, raw, create=True)
    return source, directory


# 在独立进程中提取和渲染 PDF，取消或超时必须终止工作进程
async def read_pdf(
    path: Path, pages: str | None, mode: str, session_dir: Path | None
) -> ToolResult:
    source, directory = await asyncio.to_thread(_snapshot, path, session_dir)
    process_options: dict[str, Any] = (
        {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent_lite.core.tools.pdf_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_options,
    )
    request = json.dumps(
        {"source": str(source), "directory": str(directory), "pages": pages, "mode": mode}
    ).encode()
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(request), timeout=90)
    except BaseException as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(exc, TimeoutError):
            raise FileOperationError("PDF processing timed out after 90s", "timeout") from exc
        raise
    if process.returncode:
        raise FileOperationError(
            "PDF worker failed: " + stderr.decode(errors="replace")[-2000:], "pdf_error"
        )
    result = json.loads(stdout)
    if "error" in result:
        raise FileOperationError(result["error"], "pdf_error")
    content = f"PDF: {path}; total pages={result['total_pages']}\n"
    blocks: list[dict[str, Any]] = []
    for page in result["pages"]:
        number = page["page"]
        content += f"\n--- Page {number} ---\n"
        if mode != "images":
            content += page["text"] or "[No extractable text; use page images for scanned content]"
            content += "\n"
        if "image" in page:
            content += f"Page {number} image: {page['image']}\n"
            blocks.extend(
                [
                    {"type": "text", "text": f"PDF {path.name}, page {number}"},
                    {
                        "type": "image",
                        "source": {
                            "type": "file",
                            "path": page["image"],
                            "media_type": "image/jpeg",
                        },
                    },
                ]
            )
    return ToolResult(content=content, blocks=blocks)
