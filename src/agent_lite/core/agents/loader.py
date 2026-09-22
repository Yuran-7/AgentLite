from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]

AgentSource = Literal["built-in", "user", "project"]


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    model: str = "inherit"
    max_turns: int | None = None
    background: bool = False
    source: AgentSource = "built-in"


@dataclass(frozen=True)
class AgentLoadDiagnostic:
    path: Path
    error: str


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_ALLOWED_KEYS = {
    "name",
    "description",
    "tools",
    "disallowedTools",
    "model",
    "maxTurns",
    "background",
}


def _normalize_tool(name: str) -> str:
    normalized = name.strip()
    return "shell" if normalized == "bash" else normalized


def _parse_tools(value: Any, field_name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raw = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        raw = [item.strip() for item in value if item.strip()]
    else:
        raise ValueError(f"{field_name} must be a string or an array of strings")
    return tuple(dict.fromkeys(_normalize_tool(item) for item in raw))


def parse_agent_markdown(path: Path, source: AgentSource) -> AgentDefinition:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise ValueError("agent file must start with YAML frontmatter")
    try:
        raw = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("frontmatter must be a YAML mapping")

    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unsupported frontmatter fields: {', '.join(sorted(unknown))}")

    name = raw.get("name")
    description = raw.get("description")
    body = text[match.end():].strip()
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name.strip()):
        raise ValueError("name is required and must contain only letters, digits, '-' or '_'")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required and must be non-empty")
    if not body:
        raise ValueError("agent system prompt body must be non-empty")

    tools = _parse_tools(raw.get("tools"), "tools")
    disallowed_tools = _parse_tools(raw.get("disallowedTools"), "disallowedTools") or ()

    model = raw.get("model", "inherit")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")

    max_turns = raw.get("maxTurns")
    if max_turns is not None and (
        isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0
    ):
        raise ValueError("maxTurns must be a positive integer")

    background = raw.get("background", False)
    if not isinstance(background, bool):
        raise ValueError("background must be a boolean")

    return AgentDefinition(
        name=name.strip(),
        description=description.strip(),
        system_prompt=body,
        tools=tools,
        disallowed_tools=disallowed_tools,
        model=model.strip(),
        max_turns=max_turns,
        background=background,
        source=source,
    )


_READ_ONLY_TOOLS = ("read_file", "list_dir", "web_search", "web_fetch", "cosil_localize")


def built_in_agents() -> tuple[AgentDefinition, ...]:
    return (
        AgentDefinition(
            name="general-purpose",
            description="General-purpose agent for complex research and implementation tasks",
            system_prompt=(
                "You are a general-purpose subagent. Complete the assigned task autonomously. "
                "Inspect the actual workspace before making claims, stay within the "
                "requested scope, "
                "and report concrete results and verification."
            ),
        ),
        AgentDefinition(
            name="explore",
            description=(
                "Read-only agent for locating code and understanding how the project works"
            ),
            system_prompt=(
                "You are a read-only exploration specialist. Search and inspect the "
                "workspace, trace "
                "relevant control and data flow, and return concise findings with file locations. "
                "Do not modify files."
            ),
            tools=_READ_ONLY_TOOLS,
        ),
        AgentDefinition(
            name="plan",
            description=(
                "Read-only planning agent for producing implementation-ready technical plans"
            ),
            system_prompt=(
                "You are a read-only software planning specialist. Ground the plan in the actual "
                "workspace and produce a decision-complete implementation plan covering "
                "interfaces, "
                "edge cases, and tests. Do not modify files."
            ),
            tools=_READ_ONLY_TOOLS,
        ),
    )


class AgentRegistry:
    def __init__(self, workspace_root: Path | None = None) -> None:
        self._agents: dict[str, AgentDefinition] = {
            agent.name: agent for agent in built_in_agents()
        }
        self.diagnostics: list[AgentLoadDiagnostic] = []
        self._load_dir(Path("~/.agentlite/agents").expanduser(), "user")
        if workspace_root is not None:
            self._load_dir(workspace_root / ".agentlite" / "agents", "project")

    def _load_dir(self, directory: Path, source: AgentSource) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.md")):
            try:
                agent = parse_agent_markdown(path, source)
            except (OSError, ValueError) as exc:
                self.diagnostics.append(AgentLoadDiagnostic(path=path, error=str(exc)))
                continue
            self._agents[agent.name] = agent

    def get(self, name: str) -> AgentDefinition | None:
        return self._agents.get(name)

    def list_all(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    def effective_tools(self, agent: AgentDefinition, global_cap: set[str]) -> list[str]:
        allowed = set(global_cap) if agent.tools is None else global_cap.intersection(agent.tools)
        allowed.difference_update(agent.disallowed_tools)
        return sorted(allowed)
