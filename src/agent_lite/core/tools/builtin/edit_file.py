from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent_lite.core.tools.base import BaseTool, ToolResult
from agent_lite.core.tools.file_operations import FileOperationService, finish_commit


class EditFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    old_string: str = Field(min_length=1)
    new_string: str
    replace_all: bool = False


class EditFileTool(BaseTool):
    name = "edit_file"
    params_model = EditFileParams
    description = (
        "Replace exact text in a UTF-8 file up to 1 MiB. Read each target first. "
        "The file must be unchanged since read. old_string must match once unless "
        "replace_all is true. Preserves UTF-8 BOM and file line endings."
    )
    input_schema = EditFileParams.model_json_schema()

    # 注入与读取和写入工具共享的服务
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

    # 等待精确编辑提交完成，再返回真实写入结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = EditFileParams.model_validate(params)
        content = await finish_commit(
            lambda: self.service.edit(
                parsed.path, parsed.old_string, parsed.new_string, parsed.replace_all
            )
        )
        return ToolResult(content=content)
