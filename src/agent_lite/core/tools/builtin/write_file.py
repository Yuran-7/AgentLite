from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agent_lite.core.tools.base import BaseTool, ToolResult
from agent_lite.core.tools.file_operations import (
    FileOperationError,
    FileOperationService,
    finish_commit,
)


class WriteFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    content: str


class WriteFileTool(BaseTool):
    params_model = WriteFileParams
    name = "write_file"
    description = (
        "Create a UTF-8 text file or replace its complete content (limit 1 MiB). "
        "Existing files must be fully read in this Agent context and unchanged since read. "
        "Partial reads, searches and output previews do not permit overwriting. "
        "Use edit_file for small changes. Paths resolve from the current working directory."
    )
    input_schema = WriteFileParams.model_json_schema()

    # 注入与读取和编辑工具共享的服务
    def __init__(
        self, working_directory: Path | None = None, service: FileOperationService | None = None
    ) -> None:
        self.service = service or FileOperationService(working_directory)

    # 关联当前 run 和工具调用的历史记录
    def set_call_context(self, run_id: str, tool_call_id: str) -> None:
        self.service.metadata.update(run_id=run_id, tool_call_id=tool_call_id)

    # 压缩后清空本 Agent 阅读状态
    def clear_read_context(self) -> None:
        self.service.context.clear()

    # 执行严格覆盖，文件错误不进入普通运行时自动重试
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = WriteFileParams.model_validate(params)
        try:
            content = await finish_commit(lambda: self.service.write(parsed.path, parsed.content))
            return ToolResult(content=content)
        except FileOperationError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type=exc.error_type)
