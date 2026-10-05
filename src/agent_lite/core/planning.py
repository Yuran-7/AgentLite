from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from agent_lite.core.bus.events import UserInputRequestedEvent, UserInputResolvedEvent
from agent_lite.core.events.bus import EventBus
from agent_lite.core.tools.base import BaseTool, ToolResult

PLAN_PROMPT = """You are in Plan Mode until the collaboration mode explicitly changes.
Work in three phases: explore the repository first, clarify intent and tradeoffs,
then produce a decision-complete implementation plan. Do not implement changes.
Use read_file, list_dir and web tools to discover facts before asking the user.
Use request_user_input for important preferences that cannot be discovered.
Ask exactly ONE question per request_user_input call, with two or three choices.
Wait for the answer, then reconsider the plan using that answer before deciding
whether another question is needed. Never pre-generate a batch of questions or
multiple request_user_input calls in one model response. Do not always ask three
questions: stop as soon as the important decisions are resolved.
Put the recommended choice first with '(Recommended)' in its label. The UI adds
free text automatically. Do not ask questions answerable by repository inspection.
User requests to execute while in this mode mean plan the execution.
Do not use update_plan: it is an execution checklist, not Plan Mode.
Only finalize when the plan is decision complete. Wrap the final Markdown plan
in exactly one <proposed_plan> block, with opening and closing tags on their own
lines. Include a title, summary, implementation changes, tests, and assumptions.
Revisions must produce a complete replacement plan. Do not ask whether to proceed.
"""


class InputOption(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1000)


class InputQuestion(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=80)
    header: str = Field(min_length=1, max_length=12)
    question: str = Field(min_length=1, max_length=2000)
    options: list[InputOption] = Field(min_length=2, max_length=3)


class RequestInputParams(BaseModel):
    questions: list[InputQuestion] = Field(min_length=1, max_length=1)

    # 拒绝重复问题标识，确保答案可以准确映射。
    @model_validator(mode="after")
    def unique_ids(self) -> RequestInputParams:
        if len({q.id for q in self.questions}) != len(self.questions):
            raise ValueError("question ids must be unique")
        return self


class UserInputManager:
    # 建立仅存活于当前运行的待回答问题注册表。
    def __init__(self) -> None:
        self.pending: dict[
            str, tuple[str, RequestInputParams, asyncio.Future[dict[str, Any]]]
        ] = {}

    # 发布问题并等待显式回答，取消运行时始终清理注册表。
    async def ask(self, bus: EventBus, run_id: str, session_id: str,
                  params: RequestInputParams) -> dict[str, Any]:
        request_id = uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = (session_id, params, future)
        try:
            await bus.publish(UserInputRequestedEvent(
                run_id=run_id, session_id=session_id, request_id=request_id,
                questions=[q.model_dump() for q in params.questions],
                ts=datetime.now(UTC).isoformat(),
            ))
            result = await future
            return result
        finally:
            self.pending.pop(request_id, None)
            await bus.publish(UserInputResolvedEvent(
                run_id=run_id, session_id=session_id, request_id=request_id,
                ts=datetime.now(UTC).isoformat(),
            ))

    # 校验会话归属和完整答案，拒绝过期或重复提交。
    def respond(self, session_id: str, request_id: str, answers: dict[str, str]) -> None:
        pending = self.pending.get(request_id)
        if pending is None or pending[0] != session_id or pending[2].done():
            raise ValueError("input request is no longer pending in this session")
        if set(answers) != {q.id for q in pending[1].questions}:
            raise ValueError("answer every question exactly once")
        if any(not a.strip() or len(a) > 10000 for a in answers.values()):
            raise ValueError("answers must be non-empty and at most 10000 characters")
        pending[2].set_result({"answers": {
            key: {"answers": [value.strip()]} for key, value in answers.items()
        }})


class RequestUserInputTool(BaseTool):
    name = "request_user_input"
    description = (
        "Ask exactly one multiple-choice question and wait for the user's response. "
        "Reconsider the answer before asking the next question in a new model turn. "
        "Only available in Plan Mode."
    )
    params_model = RequestInputParams
    input_schema = RequestInputParams.model_json_schema()
    waits_for_user = True

    # 注入运行专属总线和全局问答协调器。
    def __init__(self, manager: UserInputManager, bus: EventBus,
                 run_id: str, session_id: str) -> None:
        self.manager, self.bus = manager, bus
        self.run_id, self.session_id = run_id, session_id

    # 将结构化用户答案作为工具结果交回模型。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        result = await self.manager.ask(
            self.bus, self.run_id, self.session_id, RequestInputParams.model_validate(params)
        )
        return ToolResult(content=json.dumps(result, ensure_ascii=False))
