from __future__ import annotations

from pathlib import Path

import pytest

from agent_lite.core.agents.loader import AgentRegistry, parse_agent_markdown


def test_builtin_agents_are_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    registry = AgentRegistry(tmp_path)
    assert {agent.name for agent in registry.list_all()} >= {
        "general-purpose", "explore", "plan"
    }


def test_project_markdown_overrides_builtin(tmp_path: Path) -> None:
    directory = tmp_path / ".agentlite" / "agents"
    directory.mkdir(parents=True)
    (directory / "explore.md").write_text(
        """---
name: explore
description: Project explorer
tools: read_file, list_dir
disallowedTools: [shell]
model: inherit
maxTurns: 7
background: true
---
Inspect this project without editing it.
""",
        encoding="utf-8",
    )
    agent = AgentRegistry(tmp_path).get("explore")
    assert agent is not None
    assert agent.source == "project"
    assert agent.tools == ("read_file", "list_dir")
    assert agent.disallowed_tools == ("shell",)
    assert agent.max_turns == 7
    assert agent.background is True


def test_project_agent_overrides_user_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    user_agents = home / ".agentlite" / "agents"
    project_agents = workspace / ".agentlite" / "agents"
    user_agents.mkdir(parents=True)
    project_agents.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (user_agents / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: User reviewer\n---\nUser prompt.\n",
        encoding="utf-8",
    )
    (project_agents / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Project reviewer\n---\nProject prompt.\n",
        encoding="utf-8",
    )

    agent = AgentRegistry(workspace).get("reviewer")

    assert agent is not None
    assert agent.source == "project"
    assert agent.description == "Project reviewer"


def test_explicit_empty_tools_provides_no_tools(tmp_path: Path) -> None:
    path = tmp_path / "empty.md"
    path.write_text(
        "---\nname: empty\ndescription: No tools\ntools: []\n---\nThink only.\n",
        encoding="utf-8",
    )
    agent = parse_agent_markdown(path, "project")
    assert agent.tools == ()
    assert AgentRegistry(tmp_path).effective_tools(agent, {"read_file"}) == []


def test_tools_array_and_bash_alias(tmp_path: Path) -> None:
    path = tmp_path / "reviewer.md"
    path.write_text(
        """---
name: reviewer
description: Review changes
tools: [read_file, bash]
disallowedTools: write_file
model: exact-model-id
---
Review the implementation.
""",
        encoding="utf-8",
    )
    agent = parse_agent_markdown(path, "project")
    assert agent.tools == ("read_file", "shell")
    assert agent.model == "exact-model-id"


@pytest.mark.parametrize(
    "content,error",
    [
        ("plain text", "frontmatter"),
        ("---\nname: x\ndescription: y\nunknown: z\n---\nbody", "unsupported"),
        ("---\nname: x\ndescription: y\n---\n", "body"),
        ("---\nname: x\ndescription: y\nmaxTurns: 0\n---\nbody", "maxTurns"),
    ],
)
def test_invalid_agent_files(tmp_path: Path, content: str, error: str) -> None:
    path = tmp_path / "invalid.md"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        parse_agent_markdown(path, "project")


def test_invalid_file_is_diagnostic_not_fatal(tmp_path: Path) -> None:
    directory = tmp_path / ".agentlite" / "agents"
    directory.mkdir(parents=True)
    (directory / "broken.md").write_text("not frontmatter", encoding="utf-8")
    registry = AgentRegistry(tmp_path)
    assert registry.get("general-purpose") is not None
    assert len(registry.diagnostics) == 1


def test_effective_tools_are_intersection_minus_denylist(tmp_path: Path) -> None:
    directory = tmp_path / ".agentlite" / "agents"
    directory.mkdir(parents=True)
    (directory / "worker.md").write_text(
        """---
name: worker
description: Worker
tools: [read_file, write_file, web_search]
disallowedTools: [write_file]
---
Work carefully.
""",
        encoding="utf-8",
    )
    registry = AgentRegistry(tmp_path)
    agent = registry.get("worker")
    assert agent is not None
    assert registry.effective_tools(agent, {"read_file", "write_file"}) == ["read_file"]
