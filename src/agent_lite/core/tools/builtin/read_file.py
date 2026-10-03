from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_lite.core.tools.base import BaseTool, ToolResult
from agent_lite.core.tools.file_operations import (
    MAX_FILE_BYTES,
    FileOperationError,
    FileOperationService,
    path_key,
    version,
)

_MAX_BYTES = 512 * 1024


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    search: str | None = None
    max_matches: int = Field(default=50, ge=1, le=500)
    force: bool = False
    pages: str | None = None
    pdf_mode: Literal["text", "images", "both"] = "both"

    @model_validator(mode="after")
    # 拒绝倒置行范围和混用 PDF 页码与文本参数
    def validate_range(self) -> ReadFileParams:
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        if (self.pages is not None or "pdf_mode" in self.model_fields_set) and (
            self.start_line != 1 or self.end_line is not None or self.search is not None
        ):
            raise ValueError("PDF page parameters cannot be combined with text ranges or search")
        return self


class ReadFileTool(BaseTool):
    params_model = ReadFileParams
    name = "read_file"
    description = (
        "Read UTF-8 text or PDF pages. Text output is limited to 512 KiB; use small line "
        "ranges or literal search. Repeated unchanged text reads return a short notice; "
        "use force=true to resend content. Output previews are not full reads. "
        "PDF: pages is a 1-based page or range (e.g. '1-5'), at most 5 pages per call. "
        "pdf_mode=text|images|both (default both). PDFs longer than 5 pages require pages."
    )
    input_schema = ReadFileParams.model_json_schema()

    # 注入共享服务，但保留工具独立构造接口
    def __init__(
        self, working_directory: Path | None = None, service: FileOperationService | None = None
    ) -> None:
        self.service = service or FileOperationService(working_directory)

    # 压缩后使全部去重记录及阅读资格失效
    def clear_read_context(self) -> None:
        self.service.context.clear()

    # 分发 PDF 与文本读取，并只对可修改大小的文本登记阅读资格
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = ReadFileParams.model_validate(params)
        path = self.service.resolve(parsed.path)
        if not stat.S_ISREG(path.stat().st_mode):
            raise FileOperationError("Only regular files can be read", "file_validation")
        if path.suffix.lower() == ".pdf":
            if parsed.start_line != 1 or parsed.end_line is not None or parsed.search is not None:
                raise FileOperationError("Use pages for PDF files", "file_validation")
            from agent_lite.core.tools.pdf_reader import read_pdf

            directory = self.service.history.directory.parent if self.service.history else None
            return await read_pdf(path, parsed.pages, parsed.pdf_mode, directory)
        if parsed.pages is not None or "pdf_mode" in parsed.model_fields_set:
            raise FileOperationError("pages/pdf_mode only apply to PDF files", "file_validation")
        return await asyncio.to_thread(self._read_text, path, parsed)

    # 按原始版本去重，在小文件中计算实际交付的字符覆盖区间
    def _read_text(self, path: Path, parsed: ReadFileParams) -> ToolResult:
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            if parsed.start_line != 1 or parsed.end_line is not None or parsed.search is not None:
                return self._read_large_lines(path, parsed)
            return ToolResult(
                content=raw[:_MAX_BYTES].decode("utf-8", errors="replace") + "\n[truncated]",
                truncated=True,
            )
        digest = version(raw) or ""
        text = raw.decode("utf-8-sig", errors="replace")
        query = json.dumps([parsed.start_line, parsed.end_line, parsed.search, parsed.max_matches])
        context = self.service.context
        state = context.states.get(path_key(path))
        if (
            not parsed.force
            and state is not None
            and state.digest == digest
            and query in state.queries
        ):
            location = state.queries[query]
            return ToolResult(
                content=f"[file_unchanged] {path}: requested content is unchanged. "
                "Use force=true to read it again."
                + (f"\nFull tool output saved to: {location}" if location else "")
            )
        spans: list[tuple[int, int]] = []
        clipped = False
        ranged = parsed.start_line != 1 or parsed.end_line is not None or parsed.search is not None
        if ranged:
            parts: list[str] = []
            used = matches = offset = 0
            for number, line in enumerate(text.splitlines(keepends=True), 1):
                start, offset = offset, offset + len(line)
                if number < parsed.start_line:
                    continue
                if parsed.end_line is not None and number > parsed.end_line:
                    break
                if parsed.search is not None and parsed.search not in line:
                    continue
                if parsed.search is not None and matches >= parsed.max_matches:
                    parts.append(f"[More matches omitted; continue with start_line={number}]\n")
                    clipped = True
                    break
                rendered = f"{number}: {line.rstrip(chr(10)).rstrip(chr(13))}\n"
                size = len(rendered.encode("utf-8"))
                if used + size > _MAX_BYTES:
                    parts.append(
                        f"[Line {number} and subsequent content omitted: byte limit exceeded]\n"
                    )
                    clipped = True
                    break
                parts.append(rendered)
                spans.append((start, offset))
                used += size
                matches += 1
            content = "".join(parts) or "[No matching lines in the requested range]"
            if not text and parsed.search is None:
                spans = [(0, 0)]
        else:
            clipped = len(raw) > _MAX_BYTES
            content = raw[:_MAX_BYTES].decode(
                "utf-8-sig", errors="ignore" if clipped else "replace"
            )
            spans = [(0, len(content))]
            if clipped:
                content += "\n[truncated]"
        epoch = context.epoch

        # 只登记实际返回的完整区间；首尾预览不能提供任何覆盖资格
        def delivered(truncated: bool, output_path: str | None) -> None:
            context.delivered(
                path,
                digest,
                len(text),
                query,
                spans,
                parsed.search is None,
                truncated,
                output_path,
                epoch,
            )

        return ToolResult(content=content, on_delivered=delivered, truncated=clipped)

    # 大文件保持流式搜索和按行读取，不计算全文件哈希或登记修改资格
    def _read_large_lines(self, path: Path, params: ReadFileParams) -> ToolResult:
        parts: list[str] = []
        used = matches = 0
        clipped = False
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                if number < params.start_line:
                    continue
                if params.end_line is not None and number > params.end_line:
                    break
                if params.search is not None and params.search not in line:
                    continue
                if params.search is not None and matches >= params.max_matches:
                    parts.append(f"[More matches omitted; continue with start_line={number}]\n")
                    clipped = True
                    break
                rendered = f"{number}: {line.rstrip(chr(10)).rstrip(chr(13))}\n"
                size = len(rendered.encode("utf-8"))
                if used + size > _MAX_BYTES:
                    parts.append(
                        f"[Line {number} and subsequent content omitted: byte limit exceeded]\n"
                    )
                    clipped = True
                    break
                parts.append(rendered)
                used += size
                matches += 1
        return ToolResult(
            content="".join(parts) or "[No matching lines in the requested range]",
            truncated=clipped,
        )
