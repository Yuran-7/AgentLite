from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_lite.core.config import AgentLiteConfig, _normalize_tool_names
from agent_lite.core.permissions.policy import PermissionDecision, evaluate
from agent_lite.core.runner import AgentRunner
from agent_lite.core.tools.builtin.glob import GlobTool
from agent_lite.core.tools.builtin.grep import GrepTool
from agent_lite.core.tools.builtin.search import ripgrep_path


# 功能：文件模式遵守 rg 过滤规则，包含隐藏文件，排除 Git 内部文件并按修改时间分页。
# 设计：真实运行 rg，在含中文空格的目录中构造忽略项与确定时间戳验证跨平台行为。
async def test_glob_ignores_and_pages(tmp_path: Path) -> None:
    root = tmp_path / "中文 repo"
    root.mkdir()
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "internal.py").write_text("hidden")
    (root / "ignored.py").write_text("ignored")
    os.utime(root / "ignored.py", (99, 99))
    for index, name in enumerate(["old.py", "new name.py", ".hidden.py"]):
        (root / name).write_text("hello", encoding="utf-8")
        os.utime(root / name, (100 + index, 100 + index))
    tool = GlobTool(root)
    page = await tool.invoke({"pattern": "**/*.py", "head_limit": 2})
    assert page.content.splitlines()[:2] == [".hidden.py", "new name.py"]
    assert "offset: 2" in page.content
    last = await tool.invoke({"pattern": "**/*.py", "head_limit": 2, "offset": 2})
    assert last.content == "old.py\nignored.py"
    unfiltered = await GrepTool(root).invoke({"pattern": "ignored"})
    assert unfiltered.content == ".gitignore"


# 功能：正则内容搜索支持过滤、忽略大小写、上下文行、计数及无匹配结果。
# 设计：真实搜索可控文本，分别断言三种模式而不是仅验证参数拼接。
async def test_grep_modes_and_context(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("before\nHello 123\nhello 456\nafter\n", encoding="utf-8")
    (tmp_path / "other.md").write_text("hello 789\n", encoding="utf-8")
    tool = GrepTool(tmp_path)
    params = {"pattern": r"hello \d+", "glob": "*.py", "-i": True}
    assert (await tool.invoke(params)).content == "a.py"
    content = (await tool.invoke({**params, "output_mode": "content", "-C": 1})).content
    assert "a.py:2:Hello 123" in content
    assert "before" in content and "after" in content
    assert (await tool.invoke({**params, "output_mode": "count"})).content == "a.py:2"
    assert (await tool.invoke({"pattern": "missing"})).content == "No matches found."
    assert (await tool.invoke({"pattern": "hello", "type": "py"})).content == "a.py"


# 功能：内容搜索精确分页、接受单文件路径和跨行正则，非法正则明确失败。
# 设计：固定单文件匹配顺序，检查分页无重复与异常路径，不调用任何模型接口。
async def test_grep_pagination_multiline_and_invalid_regex(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hit1\nhit2\nhit3\n", encoding="utf-8")
    tool = GrepTool(tmp_path)
    params = {"pattern": "hit", "path": "a.txt", "output_mode": "content", "head_limit": 2}
    first = (await tool.invoke(params)).content
    assert "a.txt:1:hit1" in first and "a.txt:2:hit2" in first
    assert "hit3" not in first and "offset: 2" in first
    last = (await tool.invoke({**params, "offset": 2})).content
    assert last == "a.txt:3:hit3"
    multiline = await tool.invoke({"pattern": "hit1.*hit2", "multiline": True})
    assert multiline.content == "a.txt"
    with pytest.raises(ValueError, match="regex"):
        await tool.invoke({"pattern": "["})
    with pytest.raises(PermissionError):
        await tool.invoke({"pattern": "x", "path": "../"})


# 功能：搜索工具在注册及权限中取代 list_dir，旧配置自动迁移。
# 设计：使用真实注册表和权限函数，避免仅对源码文本作断言。
def test_search_registration_and_permissions(tmp_path: Path) -> None:
    runner = AgentRunner(AgentLiteConfig(), events_file=tmp_path / "events.jsonl")
    registry = runner._build_registry(workspace_root=tmp_path)
    assert registry.get("list_dir") is None
    assert registry.get("grep") is not None and registry.get("glob") is not None
    assert evaluate("grep", {"pattern": "hello"}) == PermissionDecision.ALLOW
    assert evaluate("glob", {"pattern": "*.py"}) == PermissionDecision.ALLOW
    assert _normalize_tool_names(["list_dir", "bash", "grep"]) == ["glob", "grep", "shell"]


# 功能：缺失或无效可执行文件路径提供可操作的安装提示。
# 设计：覆盖显式配置校验与无安装环境，不依赖测试机恰好存在的 rg。
def test_missing_ripgrep(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTLITE_RG", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError, match="AGENTLITE_RG"):
        ripgrep_path()
    monkeypatch.delenv("AGENTLITE_RG")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("agent_lite.core.tools.builtin.search.shutil.which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="winget"):
        ripgrep_path()
