from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_lite.core.tools.base import BaseTool, ToolResult
from agent_lite.core.tools.working_directory import resolve_tool_path

_MAX_BYTES = 512 * 1024  # 512 KB


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    search: str | None = None
    max_matches: int = Field(default=50, ge=1, le=500)

    # 拒绝倒置行范围，避免错误范围产生误导性的空结果
    @model_validator(mode="after")
    def validate_range(self) -> ReadFileParams:
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ReadFileTool(BaseTool):
    params_model = ReadFileParams
    name = "read_file"
    description = (
        "Read the text content of a file. "
        "Path may be absolute or relative to the current working directory. "
        "Files larger than 512 KB are truncated."
        " Use start_line/end_line for a small line range, or search for literal text. "
        "For persisted tool outputs, search or read a small range instead of the whole file."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path, or path relative to current working directory.",
            },
            "start_line": {"type": "integer", "minimum": 1, "description": "First line, 1-based."},
            "end_line": {"type": "integer", "minimum": 1, "description": "Last line, inclusive."},
            "search": {"type": "string", "description": "Literal text to find in complete lines."},
            "max_matches": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
        },
        "required": ["path"],
    }

    # 初始化可选工作目录，未设置时继续使用进程 cwd
    def __init__(self, working_directory: Path | None = None) -> None:
        self._working_directory = working_directory

    # 读取文件内容；超 512KB 截断；禁止 .. 路径遍历
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = ReadFileParams.model_validate(params)
        path_str = parsed.path

        if ".." in Path(path_str).parts:
            raise PermissionError(f"path traversal not allowed: {path_str}")

        path = resolve_tool_path(path_str, self._working_directory)
        if parsed.start_line != 1 or parsed.end_line is not None or parsed.search is not None:
            return self._read_lines(path, parsed)
        with path.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        truncated = len(raw) > _MAX_BYTES
        text = raw[:_MAX_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"

        return ToolResult(content=text)

    # 流式搜索或按行读取，返回行号及完整记录，正文仍受单次读取字节上限约束
    def _read_lines(self, path: Path, params: ReadFileParams) -> ToolResult:
        parts: list[str] = []
        used = 0
        matches = 0
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
                    break
                rendered = f"{number}: {line.rstrip(chr(10)).rstrip(chr(13))}\n"
                size = len(rendered.encode("utf-8"))
                if used + size > _MAX_BYTES:
                    parts.append(
                        f"[Line {number} and subsequent content omitted: byte limit exceeded]\n"
                    )
                    break
                parts.append(rendered)
                used += size
                matches += 1
        return ToolResult(content="".join(parts) or "[No matching lines in the requested range]")
