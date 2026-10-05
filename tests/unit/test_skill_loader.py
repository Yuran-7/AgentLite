from __future__ import annotations

from pathlib import Path

import pytest

from agent_lite.core.skills.loader import Skill, SkillLoader


# 功能：内建 review skill 应能被 SkillLoader 查找到
# 设计：直接调用 resolve("review")，不依赖文件系统之外的任何状态
def test_builtin_skill_found() -> None:
    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.name == "review"
    assert "审查" in skill.description or "review" in skill.description.lower()
    assert skill.system_prompt_template != ""


# 功能：全部内建 skill 均可被加载器找到
# 设计：参数化列举通用 skill 与两种多智能体工作流，防止打包时遗漏文件
@pytest.mark.parametrize(
    "name",
    ["init", "review", "skill-creator"],
)
def test_all_builtin_skills_found(name: str) -> None:
    loader = SkillLoader()
    skill = loader.resolve(name)
    assert skill is not None, f"builtin skill '{name}' not found"


# 功能：已被专用工作流替代的 orchestrate skill 不再暴露
# 设计：直接解析旧名称并断言为空，避免斜杠菜单继续出现废弃入口
def test_orchestrate_skill_removed() -> None:
    loader = SkillLoader()
    assert loader.resolve("orchestrate") is None


# 功能：多智能体 skill 应限制父 Agent 只能使用编排工具
# 设计：解析真实内建文件并比较工具集合，避免父 Agent 绕过角色直接修改工作区
@pytest.mark.parametrize("name", ["chatdev", "metagpt"])
def test_removed_multi_agent_skills_are_not_found(name: str) -> None:
    loader = SkillLoader()
    assert loader.resolve(name) is None


# 功能：不存在的 skill 名应返回 None
# 设计：查找一个不存在的名称，断言 resolve 返回 None 而非抛异常
def test_unknown_skill_returns_none() -> None:
    loader = SkillLoader()
    result = loader.resolve("nonexistent_skill_xyz")
    assert result is None


# 功能：render_prompt 应将 $ARGUMENTS 替换为传入的参数字符串
# 设计：构造含 $ARGUMENTS 的 skill，验证 render_prompt 结果不含 "$ARGUMENTS" 且含参数值
def test_arguments_substituted() -> None:
    loader = SkillLoader()
    skill = Skill(
        name="test",
        description="test skill",
        system_prompt_template="Review this: $ARGUMENTS\nPlease be thorough.",
        allowed_tools=[],
    )
    rendered = loader.render_prompt(skill, "src/foo.py")
    assert "$ARGUMENTS" not in rendered
    assert "src/foo.py" in rendered


# 功能：frontmatter 中的 allowed_tools 列表应被正确解析
# 设计：构造含 allowed_tools 的 Markdown 文件，通过 _parse_skill_file 解析并验证结果
def test_frontmatter_parsed(tmp_path: Path) -> None:
    from agent_lite.core.skills.loader import _parse_skill_file

    content = """\
---
name: custom
description: 自定义 skill 测试
allowed_tools:
  - read_file
  - bash
---
你是一个测试助手，目标：$ARGUMENTS
"""
    p = tmp_path / "custom.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.name == "custom"
    assert skill.description == "自定义 skill 测试"
    assert "read_file" in skill.allowed_tools
    assert "shell" in skill.allowed_tools
    assert "bash" not in skill.allowed_tools
    assert "$ARGUMENTS" in skill.system_prompt_template


# 功能：无 frontmatter 的 Markdown 文件仍可加载，allowed_tools 为空列表
# 设计：写入纯正文 Markdown，断言解析成功且 allowed_tools=[]
def test_no_frontmatter(tmp_path: Path) -> None:
    from agent_lite.core.skills.loader import _parse_skill_file

    content = "你是助手，请帮助用户完成任务：$ARGUMENTS\n"
    p = tmp_path / "plain.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.name == "plain"
    assert skill.allowed_tools == []
    assert "你是助手" in skill.system_prompt_template


# 功能：项目本地 skill 应覆盖内建同名 skill
# 设计：在 .agentlite/skills/ 中写入同名文件，用 monkeypatch 修改 cwd，断言加载到的是本地版本
def test_project_overrides_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_skills = tmp_path / ".agentlite" / "skills"
    local_skills.mkdir(parents=True)
    (local_skills / "review.md").write_text(
        "---\nname: review\ndescription: local override\n---\nlocal system prompt $ARGUMENTS\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.description == "local override"
    assert "local system prompt" in skill.system_prompt_template


# 功能：指定工作区时只读取该工作区的 SKILL.md，并由项目版本覆盖内建版本。
# 设计：在独立工作区建立目录式技能，验证元数据、来源、参数和名称匹配。
def test_workspace_skill_directory(tmp_path: Path) -> None:
    folder = tmp_path / ".agentlite" / "skills" / "review"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: review\ndescription: Workspace review\nallowed-tools: [read_file]\n"
        "---\nCheck the code.\n", encoding="utf-8",
    )
    loader = SkillLoader()
    skill = loader.resolve("review", tmp_path)
    assert skill is not None
    assert skill.description == "Workspace review"
    assert skill.source == "workspace"
    assert skill.allowed_tools == ["read_file"]
    assert "User arguments: src" in loader.render_prompt(skill, "src")
    assert loader.resolve("../review", tmp_path) is None


# 功能：验证其他工具和共享目录的技能既不列出也不能解析，且不覆盖内建技能。
# 设计：隔离用户目录与工作区，在各外部来源放入独有技能和同名覆盖文件以检测扫描泄漏。
def test_external_skill_directories_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for root in (home, workspace):
        for directory in (".codex", ".claude", ".agents"):
            folder = root / directory / "skills" / "external"
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(
                "---\nname: external\ndescription: External skill\n---\nExternal prompt\n",
                encoding="utf-8",
            )
            (folder.parent / "review.md").write_text(
                "---\nname: review\ndescription: External override\n---\nExternal prompt\n",
                encoding="utf-8",
            )
    folder = home / ".agentlite" / "skills" / "personal"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("Personal prompt\n", encoding="utf-8")
    loader = SkillLoader()
    assert set(loader.list_all(workspace)) == {"init", "review", "skill-creator", "personal"}
    assert loader.resolve("external", workspace) is None
    review = loader.resolve("review", workspace)
    assert review is not None
    assert review.source == "builtin"
    personal = loader.resolve("personal", workspace)
    assert personal is not None
    assert personal.source == "user"
