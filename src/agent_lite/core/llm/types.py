from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UsageStats:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    context_pct: float = 0.0  # 最近一次输入加输出的占用比例，展示时乘 100
    total_input_tokens: int | None = None  # 含缓存的完整输入，由 provider 归一化
    context_tokens: int = 0
    context_window: int = 0
    context_window_estimated: bool = False
    reasoning_output_tokens: int | None = None


@dataclass
class ToolCallBlock:
    id: str
    name: str
    input: dict[str, object]


@dataclass
class LlmResponse:
    stop_reason: str  # "end_turn" | "tool_use"
    tool_calls: list[ToolCallBlock] = field(default_factory=list)
    text: str = ""
    usage: UsageStats | None = None
    # thinking blocks from extended thinking — must be preserved verbatim in conversation history
    thinking_blocks: list[dict[str, object]] = field(default_factory=list)
    content_blocks: list[dict[str, object]] | None = None  # 保留 Anthropic 原始块顺序
