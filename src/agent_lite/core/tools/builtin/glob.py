from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent_lite.core.tools.base import BaseTool, ToolResult
from agent_lite.core.tools.builtin.search import paged_text, run_rg, search_target


class GlobParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pattern: str = Field(min_length=1, description="File glob, for example **/*.py or src/**")
    path: str = "."
    head_limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0, le=100_000)


class GlobTool(BaseTool):
    name = "glob"
    params_model = GlobParams
    description = (
        "Find files by glob pattern using ripgrep, respecting ignore files and excluding .git. "
        "Use this instead of list_dir or shell find. Returns paths relative to the search root, "
        "newest files first. Supports head_limit/offset pagination; use grep for file contents."
    )
    input_schema = GlobParams.model_json_schema()

    # 保存文件匹配使用的工作区目录。
    def __init__(self, working_directory: Path | None = None) -> None:
        self._working_directory = working_directory

    # 枚举符合模式的文件并返回最新文件优先的分页结果。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = GlobParams.model_validate(params)
        root, target = search_target(p.path, self._working_directory)
        if not (root / target).is_dir():
            raise NotADirectoryError("glob path must be a directory")
        args = ["--files", "--null", "--sortr", "modified", "--glob", p.pattern, "--", target]
        data = await run_rg(args, root, p.offset + p.head_limit + 1, b"\0")
        return ToolResult(content=paged_text(data, b"\0", p.head_limit, p.offset))
