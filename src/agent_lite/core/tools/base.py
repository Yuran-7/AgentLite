from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied"
    error_type: str | None = None
    blocks: list[dict[str, Any]] = field(default_factory=list)
    on_delivered: Callable[[bool, str | None], None] | None = None
    truncated: bool = False
    delivery_truncated: bool = False
    output_path: str | None = None

    # 生成模型内容，图片只保存会话资产引用
    def model_content(self) -> str | list[dict[str, Any]]:
        if not self.blocks:
            return self.content
        return [{"type": "text", "text": self.content}, *self.blocks]

    # 后续模型请求成功响应后确认实际交付
    def confirm_delivery(self) -> None:
        if self.on_delivered is not None:
            self.on_delivered(self.delivery_truncated, self.output_path)
            self.on_delivered = None


class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict[str, object]
    params_model: ClassVar[type[BaseModel] | None] = None

    # 执行工具调用，返回结果或错误
    @abstractmethod
    async def invoke(self, params: dict[str, object]) -> ToolResult: ...

    # 文件工具覆盖此方法关联本次提交标识
    def set_call_context(self, run_id: str, tool_call_id: str) -> None:
        return None

    # 文件工具覆盖此方法清除压缩前的阅读资格
    def clear_read_context(self) -> None:
        return None

    # 有状态工具可覆盖此方法释放连接等资源
    async def aclose(self) -> None:
        return None
