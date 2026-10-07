from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_lite.core.tools.base import BaseTool, ToolResult
from agent_lite.core.tools.builtin.search import paged_text, run_rg, search_target


class GrepParams(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    pattern: str = Field(min_length=1, description="ripgrep regular expression to search")
    path: str = "."
    glob: str | None = None
    type: str | None = None
    output_mode: Literal["content", "files_with_matches", "count"] = "files_with_matches"
    before: int = Field(default=0, ge=0, le=100, alias="-B")
    after: int = Field(default=0, ge=0, le=100, alias="-A")
    context: int = Field(default=0, ge=0, le=100, alias="-C")
    ignore_case: bool = Field(default=False, alias="-i")
    line_numbers: bool = Field(default=True, alias="-n")
    multiline: bool = False
    head_limit: int = Field(default=250, ge=1, le=1000)
    offset: int = Field(default=0, ge=0, le=100_000)


class GrepTool(BaseTool):
    name = "grep"
    params_model = GrepParams
    description = (
        "Search file contents using ripgrep regular expressions, respecting ignore files. "
        "Use this instead of shell grep/rg. Supports file filters, context lines, multiline, "
        "content/files_with_matches/count modes and head_limit/offset pagination. "
        "Paths are relative to the search root. Use read_file before editing matches."
    )
    input_schema = GrepParams.model_json_schema()

    # 保存模型搜索使用的工作区目录。
    def __init__(self, working_directory: Path | None = None) -> None:
        self._working_directory = working_directory

    # 将校验后的搜索参数转换为 ripgrep 参数并返回可翻页结果。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = GrepParams.model_validate(params)
        root, target = search_target(p.path, self._working_directory)
        args = ["--with-filename", "--no-heading", "--max-columns", "500",
                "--max-columns-preview"]
        separator = b"\n"
        if p.output_mode == "files_with_matches":
            args += ["--files-with-matches", "--null", "--sortr", "modified"]
            separator = b"\0"
        else:
            args += ["--sort", "path"]
            if p.output_mode == "count":
                args.append("--count")
            else:
                if p.line_numbers:
                    args.append("--line-number")
                args += ["--before-context", str(max(p.before, p.context)),
                         "--after-context", str(max(p.after, p.context))]
        if p.glob:
            args += ["--glob", p.glob]
        if p.type:
            args += ["--type", p.type]
        if p.ignore_case:
            args.append("--ignore-case")
        if p.multiline:
            args += ["--multiline", "--multiline-dotall"]
        args += ["-e", p.pattern, "--", target]
        data = await run_rg(args, root, p.offset + p.head_limit + 1, separator)
        return ToolResult(content=paged_text(data, separator, p.head_limit, p.offset))
