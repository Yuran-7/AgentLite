from __future__ import annotations

from agent_lite.core.tools.base import BaseTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    # 注册工具；同名覆盖
    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    # 按名称查找工具，不存在返回 None
    def get(self, name: str) -> BaseTool | None:
        if name == "bash":  # legacy messages may still contain the former public name
            name = "shell"
        return self._tools.get(name)

    # 返回所有工具的内部统一 schema 列表，由具体 LLM Provider 转换协议格式
    def tool_schemas(self) -> list[dict[str, object]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]

    # run 结束时释放所有有状态工具；单个工具清理失败不阻止其他工具关闭
    async def aclose(self) -> None:
        for tool in self._tools.values():
            try:
                await tool.aclose()
            except Exception:
                continue
