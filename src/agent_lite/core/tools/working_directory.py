from __future__ import annotations

from pathlib import Path


# 按可选工作目录解析工具路径，绝对路径保持原样
def resolve_tool_path(path_str: str, working_directory: Path | None) -> Path:
    path = Path(path_str)
    if working_directory is not None and not path.is_absolute():
        return working_directory / path
    return path
