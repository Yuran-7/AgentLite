from __future__ import annotations

from pathlib import Path


# 读取指定路径的 context.md，路径不存在或内容为空时返回空字符串
def load_context_file(path: Path) -> str:
    p = path.expanduser()
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8").strip()


# 按父目录到工作区根目录的顺序加载所有 AGENT.md，子目录规则覆盖父目录规则
def load_agent_context(workspace_root: Path | None) -> str:
    if workspace_root is None:
        return ""

    root = workspace_root.expanduser().resolve()
    files = [parent / "AGENT.md" for parent in reversed((root, *root.parents))]
    sections = []
    for path in files:
        content = load_context_file(path)
        if content:
            sections.append(f"# {path}\n\n{content}")
    return "\n\n".join(sections)
