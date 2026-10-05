from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Skill:
    name: str
    description: str
    system_prompt_template: str
    allowed_tools: list[str] = field(default_factory=list)
    path: Path | None = None
    source: str = ""


_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_VALID_NAME = re.compile(r"^[\w-]+$", re.UNICODE)


# 解析 Codex/Claude 风格 SKILL.md 与旧版平铺 Markdown 文件。
def _parse_skill_file(path: Path) -> Skill:
    content = path.read_text(encoding="utf-8-sig")
    match = _FRONTMATTER_RE.match(content)
    metadata = yaml.safe_load(match.group(1)) if match else {}
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("skill frontmatter must be a mapping")
    name = path.parent.name if path.name == "SKILL.md" else path.stem
    declared = metadata.get("name", name)
    if not isinstance(declared, str) or not _VALID_NAME.fullmatch(declared):
        raise ValueError("invalid skill name")
    description = metadata.get("description", "")
    if not isinstance(description, str):
        raise ValueError("skill description must be text")
    tools = metadata.get("allowed-tools", metadata.get("allowed_tools", []))
    if isinstance(tools, str):
        tools = tools.split()
    if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
        raise ValueError("allowed tools must be a list")
    body = (content[match.end():] if match else content).strip()
    if not body:
        raise ValueError("skill body is empty")
    return Skill(declared, description.strip(), body,
                 ["shell" if tool == "bash" else tool for tool in tools], path=path)


class SkillLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 仅扫描内建和 AgentLite 专属目录，工作区覆盖用户及内建的同名 skill。
    def _directories(self, workspace_root: str | Path | None = None) -> list[tuple[Path, str]]:
        workspace = Path(workspace_root) if workspace_root else Path.cwd()
        return [(self._BUILTIN_DIR, "builtin"),
                (Path("~/.agentlite/skills").expanduser(), "user"),
                (workspace / ".agentlite/skills", "workspace")]

    # 读取当前工作区可见的 skill，并跳过损坏文件。
    def list_all_skills(self, workspace_root: str | Path | None = None) -> list[Skill]:
        found: dict[str, Skill] = {}
        for directory, source in self._directories(workspace_root):
            if not directory.is_dir():
                continue
            paths = sorted(directory.glob("*.md")) + sorted(directory.glob("*/SKILL.md"))
            for path in paths:
                try:
                    skill = _parse_skill_file(path)
                    skill.source = source
                    found[skill.name] = skill
                except (OSError, UnicodeError, yaml.YAMLError, ValueError):
                    continue
        return sorted(found.values(), key=lambda skill: skill.name)

    # 按名称查找当前工作区可见的 skill。
    def resolve(self, name: str, workspace_root: str | Path | None = None) -> Skill | None:
        if not _VALID_NAME.fullmatch(name):
            return None
        return next((skill for skill in self.list_all_skills(workspace_root)
                     if skill.name == name), None)

    # 返回可供斜杠菜单展示的名称。
    def list_all(self, workspace_root: str | Path | None = None) -> list[str]:
        return [skill.name for skill in self.list_all_skills(workspace_root)]

    # 展开调用参数，保留不带占位符的正文原样。
    def render_prompt(self, skill: Skill, arguments: str) -> str:
        body = skill.system_prompt_template.replace("$ARGUMENTS", arguments)
        if "$ARGUMENTS" not in skill.system_prompt_template and arguments:
            body += f"\n\nUser arguments: {arguments}"
        return body
