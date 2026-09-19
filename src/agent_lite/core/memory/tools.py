from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agent_lite.core.tools.base import BaseTool, ToolResult

_MAX_READ_BYTES = 512 * 1024
_MAX_WRITE_BYTES = 512 * 1024
_WRITABLE_FILES = {"MEMORY.md", "memory_summary.md"}


class _PathParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class _WriteParams(_PathParams):
    content: str


# 在指定根目录内解析严格相对路径。
def _resolve(root: Path, path_text: str) -> Path:
    relative = Path(path_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise PermissionError(f"path must stay inside the memory staging directory: {path_text}")
    resolved = (root / relative).resolve()
    if resolved != root and root not in resolved.parents:
        raise PermissionError(f"path escapes the memory staging directory: {path_text}")
    return resolved


class MemoryListTool(BaseTool):
    name = "list_memory_files"
    description = "List files available in the isolated memory staging directory."
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    # 保存隔离的 staging 根目录。
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    # 列出最多三百个可供 consolidation 使用的相对文件路径。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        paths = [
            path.relative_to(self._root).as_posix()
            for path in sorted(self._root.rglob("*"))
            if path.is_file()
        ]
        if len(paths) > 300:
            paths = paths[:300] + ["[truncated]"]
        return ToolResult(content="\n".join(paths))


class MemoryReadTool(BaseTool):
    params_model = _PathParams
    name = "read_memory_file"
    description = "Read one UTF-8 text file from the isolated memory staging directory."
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    # 保存隔离的 staging 根目录。
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    # 读取有大小上限的 staging 文本文件。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        path_text = _PathParams.model_validate(params).path
        path = _resolve(self._root, path_text)
        raw = path.read_bytes()
        truncated = len(raw) > _MAX_READ_BYTES
        text = raw[:_MAX_READ_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"
        return ToolResult(content=text)


class MemoryWriteTool(BaseTool):
    params_model = _WriteParams
    name = "write_memory_artifact"
    description = "Write MEMORY.md or memory_summary.md in the isolated staging directory."
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "enum": sorted(_WRITABLE_FILES)},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    # 保存隔离的 staging 根目录。
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    # 只允许覆盖两个正式 Phase 2 产物。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = _WriteParams.model_validate(params)
        if parsed.path not in _WRITABLE_FILES:
            raise PermissionError(f"Phase 2 cannot write {parsed.path!r}")
        encoded = parsed.content.encode("utf-8")
        if len(encoded) > _MAX_WRITE_BYTES:
            return ToolResult(
                content=f"artifact too large: {len(encoded)} bytes",
                is_error=True,
                error_type="runtime_error",
            )
        path = _resolve(self._root, parsed.path)
        path.write_text(parsed.content, encoding="utf-8")
        return ToolResult(content=f"wrote {len(encoded)} bytes to {parsed.path}")
