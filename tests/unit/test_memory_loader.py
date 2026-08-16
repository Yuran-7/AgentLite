from __future__ import annotations

from pathlib import Path

from agent_lite.core.memory.loader import load_agent_context, load_context_file


# 功能：验证文件存在时返回去除首尾空格的完整内容
# 设计：用 tmp_path 写入带前后空白行的文件，断言 strip 后内容一致
def test_load_existing_file(tmp_path: Path) -> None:
    ctx = tmp_path / "context.md"
    ctx.write_text("  # My Context\n- item one\n", encoding="utf-8")
    result = load_context_file(ctx)
    assert result == "# My Context\n- item one"


# 功能：验证文件不存在时返回空字符串
# 设计：传入不存在的路径，无需创建文件，断言返回值为空字符串
def test_load_missing_file(tmp_path: Path) -> None:
    result = load_context_file(tmp_path / "nonexistent.md")
    assert result == ""


# 功能：验证 AGENT.md 会从父目录到 workspace 根目录按层级加载
# 设计：在父目录和 workspace 根目录分别写入规则，断言顺序以及不存在的子目录未被读取
def test_load_agent_context_from_parent_to_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "repo" / "app"
    workspace.mkdir(parents=True)
    (tmp_path / "repo" / "AGENT.md").write_text("parent rule", encoding="utf-8")
    (workspace / "AGENT.md").write_text("workspace rule", encoding="utf-8")

    result = load_agent_context(workspace)

    assert result.index("parent rule") < result.index("workspace rule")
    assert "# " + str(workspace / "AGENT.md") in result


# 功能：验证文件存在但内容为空（或仅空白）时返回空字符串
# 设计：写入纯空白内容，strip 后为空，断言返回空字符串
def test_load_empty_file(tmp_path: Path) -> None:
    ctx = tmp_path / "context.md"
    ctx.write_text("   \n\n  ", encoding="utf-8")
    result = load_context_file(ctx)
    assert result == ""
