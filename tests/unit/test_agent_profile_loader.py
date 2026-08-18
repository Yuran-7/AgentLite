from __future__ import annotations

from pathlib import Path

import pytest

from agent_lite.core.agents.loader import AgentProfileLoader


# 功能：三个多智能体 skill 引用的专用角色均可加载
# 设计：参数化列举 skill 模板中的全部 subagent_type，防止角色改名或打包遗漏
@pytest.mark.parametrize(
    "role",
    [
        "debater",
        "debate-judge",
        "chatdev-ceo",
        "chatdev-cpo",
        "chatdev-cto",
        "chatdev-programmer",
        "chatdev-reviewer",
        "chatdev-tester",
        "metagpt-product-manager",
        "metagpt-architect",
        "metagpt-engineer",
        "metagpt-qa",
    ],
)
def test_multi_agent_skill_roles_found(role: str) -> None:
    loader = AgentProfileLoader()
    profile = loader.load(role)
    assert profile is not None, f"builtin role '{role}' not found"
    assert profile.system_prompt
    assert profile.allowed_tools


# 功能：未知角色名应返回 None
# 设计：查找不存在的角色，断言返回 None 而非抛异常
def test_unknown_role_returns_none() -> None:
    loader = AgentProfileLoader()
    result = loader.load("nonexistent_role_xyz")
    assert result is None


# 功能：TOML 角色配置文件应被正确解析
# 设计：写入临时 TOML 文件，通过 _parse 解析并验证所有字段
def test_toml_parsed(tmp_path: Path) -> None:
    content = """\
[agent]
description = "测试角色"
system_prompt = "你是测试助手。"
allowed_tools = ["read_file", "bash"]
model = "claude-sonnet-4-6"
"""
    p = tmp_path / "tester.toml"
    p.write_text(content, encoding="utf-8")
    loader = AgentProfileLoader()
    profile = loader._parse(p, "tester")
    assert profile.name == "tester"
    assert profile.description == "测试角色"
    assert profile.system_prompt == "你是测试助手。"
    assert "read_file" in profile.allowed_tools
    assert "shell" in profile.allowed_tools
    assert "bash" not in profile.allowed_tools
    assert profile.model == "claude-sonnet-4-6"


# 功能：项目本地角色配置应覆盖内建同名配置
# 设计：在 .agentlite/agents/ 中写入同名 TOML，monkeypatch cwd，断言加载到本地版本
def test_project_overrides_builtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_agents = tmp_path / ".agentlite" / "agents"
    local_agents.mkdir(parents=True)
    (local_agents / "debater.toml").write_text(
        '[agent]\ndescription = "local debater"\nsystem_prompt = "local prompt"\n'
        'allowed_tools = ["list_dir"]\nmodel = ""\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    loader = AgentProfileLoader()
    profile = loader.load("debater")
    assert profile is not None
    assert profile.description == "local debater"
    assert "list_dir" in profile.allowed_tools
