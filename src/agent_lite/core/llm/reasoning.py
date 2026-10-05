from __future__ import annotations

from dataclasses import replace
from typing import Literal

from agent_lite.core.config import LlmConfig

ReasoningEffort = Literal["", "none", "minimal", "low", "medium", "high", "xhigh", "max"]
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


# 返回模型及协议支持的推理档位，避免向不支持的 Claude 模型发送参数。
def supported_efforts(model: str, protocol: str = "openai") -> tuple[str, ...]:
    name = model.lower()
    if protocol in {"openai", "anthropic"}:
        if name in {"glm-5.3", "glm-5.3-flash", "kimi-k3"}:
            return ("low", "high", "max")
        if name == "deepseek-v4.1-flash":
            return ("none", "low", "high", "max")
    if protocol == "anthropic":
        if any(part in name for part in (
            "claude-opus-5", "claude-opus-4-7", "claude-opus-4-8",
            "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
        )):
            return ("low", "medium", "high", "xhigh", "max")
        if any(part in name for part in ("claude-opus-4-6", "claude-sonnet-4-6")):
            return ("low", "medium", "high", "max")
        if "claude-opus-4-5" in name:
            return ("low", "medium", "high")
        return ()
    if name.startswith(("gpt-6.1-sol", "gpt-6-astra")):
        return ("low", "medium", "high", "xhigh", "max")
    if name.startswith(("gpt-6-sol", "gpt-6-luna")):
        return ("none", "low", "medium", "high", "xhigh", "max")
    if name.startswith(("gpt-5.2", "gpt-5.4", "gpt-5.5")):
        return ("none", "low", "medium", "high", "xhigh")
    return EFFORTS


# 应用会话或模型配置中的推理强度，并按协议校验模型支持的档位。
def with_reasoning(config: LlmConfig, effort: str = "") -> LlmConfig:
    if config.protocol not in {"openai", "anthropic"}:
        return config
    selected = effort or config.reasoning_effort
    if selected and selected not in supported_efforts(config.default_model, config.protocol):
        raise ValueError("当前模型不支持该推理强度，请通过 /reasoning 选择其他档位或默认")
    return replace(config, reasoning_effort=selected)
