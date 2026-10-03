from __future__ import annotations

import base64
from pathlib import Path
from typing import Any


# 仅在发送 API 请求前展开本地图片，持久化和 trace 保留资产引用
def expand_assets(value: Any) -> Any:
    if isinstance(value, list):
        return [expand_assets(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("type") == "image":
        source = value.get("source", {})
        if source.get("type") == "file":
            try:
                raw = Path(source["path"]).read_bytes()
                if len(raw) > 3 * 1024 * 1024:
                    raise ValueError("Image asset exceeds size limit")
                return {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": source["media_type"],
                        "data": base64.b64encode(raw).decode("ascii"),
                    },
                }
            except (OSError, ValueError) as exc:
                return {"type": "text", "text": f"[Image asset unavailable: {exc}]"}
    return {key: expand_assets(item) for key, item in value.items()}
