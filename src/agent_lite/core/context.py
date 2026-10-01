from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExecutionContext:
    """单次 Agent run 的执行上下文；包含会话回放，但不负责持久化跨 run 的历史。"""

    run_id: str
    goal: str
    max_steps: int
    prefill_messages: list[dict[str, Any]] = field(default_factory=list)
    agent_context: str = ""
    workspace_root: Path | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    message_metadata: dict[int, dict[str, str]] = field(default_factory=dict)
    step: int = 0
    status: str = "running"  # "running" | "success" | "failed"
    reason: str | None = None
    result: str = ""
    # skill 或 subagent 角色可覆盖默认 system prompt
    system_prompt_override: str | None = None
    memory_context: str = ""

    # 初始化消息历史，优先使用 session 完整回放内容
    def __post_init__(self) -> None:
        if self.prefill_messages:
            self.messages = [dict(m) for m in self.prefill_messages]
        elif not self.messages:
            self.messages.append({"role": "user", "content": self.goal})

    # 返回当前 run 的 system prompt；有 override 时跳过 base，直接注入记忆层
    def system_prompt(self, base: str) -> str:
        parts = [self.system_prompt_override if self.system_prompt_override else base]
        if self.agent_context.strip():
            parts.append("\n\n## AGENT.md\n" + self.agent_context.strip())
        if self.workspace_root is not None:
            parts.append(
                "\n\n## Workspace\n"
                f"Root: {self.workspace_root}\n"
                "Resolve relative file and shell paths from this workspace root."
            )
        if self.memory_context.strip():
            parts.append("\n\n" + self.memory_context.strip())
        return "".join(parts)

    # 将 LLM 响应的 content blocks 追加为 assistant 消息
    def add_assistant_message(self, content: list[Any]) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def add_user_message(
        self,
        content: str,
        *,
        kind: str | None = None,
        notification_id: str | None = None,
    ) -> None:
        self.messages.append({"role": "user", "content": content})
        metadata: dict[str, str] = {}
        if kind is not None:
            metadata["kind"] = kind
        if notification_id is not None:
            metadata["notification_id"] = notification_id
        if metadata:
            self.message_metadata[len(self.messages) - 1] = metadata

    def persistence_messages(self, start: int) -> list[dict[str, Any]]:
        persisted: list[dict[str, Any]] = []
        for index, message in enumerate(self.messages[start:], start=start):
            row = dict(message)
            row.update(self.message_metadata.get(index, {}))
            persisted.append(row)
        return persisted

    # 将工具调用结果追加为 user 消息；同一步的多个结果共享同一条消息
    def add_tool_result(
        self, tool_use_id: str, content: str, is_error: bool = False
    ) -> None:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True

        last = self.messages[-1] if self.messages else None
        if (
            last is not None
            and last["role"] == "user"
            and isinstance(last["content"], list)
            and last["content"]
            and all(b.get("type") == "tool_result" for b in last["content"])
        ):
            last["content"].append(block)
        else:
            self.messages.append({"role": "user", "content": [block]})

    # 返回 True 表示 loop 应停止（状态不再是 running）
    def is_done(self) -> bool:
        return self.status != "running"

    # 将 run 标记为成功
    def mark_success(self) -> None:
        self.status = "success"

    # 将 run 标记为失败并记录原因
    def mark_failed(self, reason: str) -> None:
        self.status = "failed"
        self.reason = reason
