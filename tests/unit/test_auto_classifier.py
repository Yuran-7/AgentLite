from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.types import LlmResponse
from agent_lite.core.permissions.classifier import AutoModeClassifier, parse_verdict


class Provider:
    # 记录实际请求，模拟输出或超时而不连接外部服务。
    def __init__(
        self, text: str = '{"decision":"allow","reason":"safe"}', slow: bool = False
    ) -> None:
        self.text, self.slow = text, slow
        self.requests: list[dict[str, Any]] = []

    # 检查分类请求无工具并捕获其隔离总线。
    async def chat(
        self, messages: Any, tool_schemas: Any, bus: EventBus, run_id: str, **kwargs: Any
    ) -> LlmResponse:
        self.requests.append({"messages": messages, "bus": bus, "run_id": run_id})
        assert tool_schemas == []
        if self.slow:
            await asyncio.Event().wait()
        return LlmResponse("end_turn", text=self.text)


# 功能：仅接受唯一完整且字段合法的 JSON 判定。
# 设计：覆盖多对象、额外文字、非法字段与允许的代码围栏。
@pytest.mark.parametrize(
    "text,decision",
    [
        ('{"decision":"allow","reason":"safe"}', "allow"),
        ('```json\n{"decision":"allow","reason":"safe"}\n```', "allow"),
        ('text {"decision":"allow","reason":"safe"}', "block"),
        ('{"decision":"allow","reason":"safe"}{}', "block"),
        ('{"decision":"allow","reason":""}', "block"),
        ('{"decision":"yes","reason":"safe"}', "block"),
        ('{"decision":"allow","reason":"safe","extra":1}', "block"),
    ],
)
def test_strict_output(text: str, decision: str) -> None:
    assert parse_verdict(text).decision == decision


# 功能：分类调用隔离总线、限制上下文并且不缓存相同动作。
# 设计：两次相同动作捕获不同总线与请求标识，检查完整动作未被截断。
async def test_isolated_requests_and_no_cache() -> None:
    provider = Provider()
    classifier = AutoModeClassifier(lambda: provider)
    action = {"tool_name": "shell", "params": {"command": "git push"}}
    for _ in range(2):
        assert (await classifier.classify(action, ["x" * 4000], [])).decision == "allow"
    first, second = provider.requests
    assert first["bus"] is not second["bus"]
    assert first["run_id"] != second["run_id"]
    payload = json.loads(first["messages"][0]["content"])
    assert payload["action"] == action
    assert len(payload["real_user_messages"][0]) == 2000


# 功能：动作过长和缺少真实意图时直接要求人工审批。
# 设计：检查模型调用为零，确保预算降级不采用被截断的动作。
async def test_budget_and_missing_intent() -> None:
    provider = Provider()
    classifier = AutoModeClassifier(lambda: provider, max_input_chars=200)
    assert (await classifier.classify({}, [], [])).reason_code == "intent_missing"
    assert (
        await classifier.classify({"command": "x" * 300}, ["do it"], [])
    ).reason_code == "classifier_input_too_large"
    assert not provider.requests


# 功能：分类超时退回人工审批，取消则向上传播。
# 设计：用永不完成的 fake provider 分别触发超时与主动取消。
async def test_timeout_and_cancel() -> None:
    provider = Provider(slow=True)
    classifier = AutoModeClassifier(lambda: provider, timeout_s=0.01)
    assert (await classifier.classify({}, ["do it"], [])).reason_code == "classifier_timeout"
    task = asyncio.create_task(AutoModeClassifier(lambda: provider).classify({}, ["do it"], []))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
