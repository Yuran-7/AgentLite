from __future__ import annotations

from typing import Any


# 将 SDK 内容块无损转换为可写入 JSONL 的字典，兼容测试替身
def block_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return dict(block)
    if callable(getattr(type(block), "model_dump", None)):
        return dict(block.model_dump(mode="json", exclude_none=True))
    return {
        key: _json_value(value)
        for key, value in vars(block).items() if not key.startswith("_")
    }


# 递归序列化测试替身中的嵌套内容，真实 SDK 对象优先使用 model_dump
def _json_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if hasattr(value, "__dict__"):
        return block_dict(value)
    return value
