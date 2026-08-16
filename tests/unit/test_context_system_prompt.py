from __future__ import annotations

from pathlib import Path

from agent_lite.core.context import ExecutionContext


def _make_ctx(**kwargs) -> ExecutionContext:
    defaults = dict(run_id="r1", goal="test goal", max_steps=5)
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


# 功能：验证三层记忆全部存在时都出现在 system prompt 中且顺序正确
# 设计：分别设置 global_context、agent_context、session_notes，断言各 section 标题及内容依次出现
def test_all_layers_present() -> None:
    ctx = _make_ctx(
        global_context="global line",
        agent_context="agent rule",
        session_notes="session note",
    )
    prompt = ctx.system_prompt("BASE")
    assert "BASE" in prompt
    assert "## Global Context\nglobal line" in prompt
    assert "## AGENT.md\nagent rule" in prompt
    assert "## Session Notes\nsession note" in prompt
    # 顺序：global 在 AGENT.md 之前，AGENT.md 在 session 之前
    assert prompt.index("Global") < prompt.index("AGENT.md") < prompt.index("Session")


# 功能：验证三层均为空时 system prompt 只含 base
# 设计：不设置任何记忆字段，断言输出等于 base
def test_no_layers() -> None:
    ctx = _make_ctx()
    prompt = ctx.system_prompt("BASE_ONLY")
    assert prompt == "BASE_ONLY"


# 功能：验证只有 global_context 时只出现 Global section，其他 section 不出现
# 设计：只设置 global_context，断言 Project 和 Session 标题不在 prompt 中
def test_only_global() -> None:
    ctx = _make_ctx(global_context="global content")
    prompt = ctx.system_prompt("BASE")
    assert "## Global Context" in prompt
    assert "## AGENT.md" not in prompt
    assert "## Session Notes" not in prompt


# 功能：验证 session_notes 非空时包含 note_save 提示语
# 设计：只设置 session_notes，断言 prompt 含 note_save 相关提示
def test_session_notes_hint() -> None:
    ctx = _make_ctx(session_notes="some note")
    prompt = ctx.system_prompt("BASE")
    assert "note_save" in prompt


# 功能：验证设置工作区时 system prompt 包含规范根目录和相对路径语义
# 设计：使用 Path 字段生成提示词，确保 Coding 检索前置上下文能明确告知模型当前项目作用域
def test_workspace_root_in_system_prompt() -> None:
    root = Path("/tmp/example-workspace")
    prompt = _make_ctx(workspace_root=root).system_prompt("BASE")

    assert "## Workspace" in prompt
    assert f"Root: {root}" in prompt
    assert "relative file and shell paths" in prompt
