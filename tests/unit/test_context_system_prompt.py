from __future__ import annotations

from pathlib import Path

from agent_lite.core.context import ExecutionContext


def _make_ctx(**kwargs) -> ExecutionContext:
    defaults = dict(run_id="r1", goal="test goal", max_steps=5)
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


# 功能：验证工作区上下文出现在 system prompt 中
# 设计：设置 agent_context，断言 AGENT.md section 被注入
def test_all_layers_present() -> None:
    ctx = _make_ctx(
        agent_context="agent rule",
    )
    prompt = ctx.system_prompt("BASE")
    assert "BASE" in prompt
    assert "## AGENT.md\nagent rule" in prompt


# 功能：验证工作区上下文为空时 system prompt 只含 base
# 设计：不设置任何记忆字段，断言输出等于 base
def test_no_layers() -> None:
    ctx = _make_ctx()
    prompt = ctx.system_prompt("BASE_ONLY")
    assert prompt == "BASE_ONLY"


# 功能：验证设置工作区时 system prompt 包含规范根目录和相对路径语义
# 设计：使用 Path 字段生成提示词，确保 Coding 检索前置上下文能明确告知模型当前项目作用域
def test_workspace_root_in_system_prompt() -> None:
    root = Path("/tmp/example-workspace")
    prompt = _make_ctx(workspace_root=root).system_prompt("BASE")

    assert "## Workspace" in prompt
    assert f"Root: {root}" in prompt
    assert "relative file and shell paths" in prompt
