from __future__ import annotations

from pathlib import Path


# 读取指定路径的 context.md，路径不存在或内容为空时返回空字符串
def load_context_file(path: Path) -> str:
    p = path.expanduser()
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8").strip()


# 只加载工作区根目录下的 AGENT.md，不向父目录或子目录递归查找
def load_agent_context(workspace_root: Path | None) -> str:
    if workspace_root is None:
        return ""

    root = workspace_root.expanduser().resolve()
    path = root / "AGENT.md"
    content = load_context_file(path)
    return f"# {path}\n\n{content}" if content else ""
