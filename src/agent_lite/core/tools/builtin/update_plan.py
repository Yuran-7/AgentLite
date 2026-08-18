from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from agent_lite.core.bus.events import PlanItem, PlanUpdatedEvent
from agent_lite.core.events.bus import EventBus
from agent_lite.core.tools.base import BaseTool, ToolResult


class UpdatePlanParams(BaseModel):
    explanation: str | None = None
    plan: list[PlanItem]


class UpdatePlanTool(BaseTool):
    name = "update_plan"
    description = (
        "Update the task plan. Provide an optional explanation and a list of plan "
        "items, each with a step and status. At most one step can be in_progress."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "explanation": {
                "type": "string",
                "description": "Optional explanation for creating or changing the plan.",
            },
            "plan": {
                "type": "array",
                "description": "The complete current plan, replacing the previous plan.",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string", "description": "Plan step text."},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["step", "status"],
                },
            },
        },
        "required": ["plan"],
    }
    params_model: ClassVar[type[BaseModel] | None] = UpdatePlanParams

    # 初始化计划事件发布器和当前 run 标识
    def __init__(self, bus: EventBus, run_id: str) -> None:
        self._bus = bus
        self._run_id = run_id

    # 校验并发布完整计划，供 TUI 和事件回放使用
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = UpdatePlanParams.model_validate(params)
        in_progress = sum(item.status == "in_progress" for item in parsed.plan)
        if in_progress > 1:
            return ToolResult(
                content="at most one plan item can be in_progress",
                is_error=True,
                error_type="runtime_error",
            )
        await self._bus.publish(
            PlanUpdatedEvent(
                run_id=self._run_id,
                explanation=parsed.explanation,
                plan=parsed.plan,
                ts=_now(),
            )
        )
        return ToolResult(content="Plan updated")


# 返回当前 UTC 时间，写入计划事件
def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
