from __future__ import annotations

import os
from pathlib import Path

import pytest

from kama_claude.core.tools.builtin.bash import (
    ShellTool,
    _shell_argv,
    _shell_description,
)
from kama_claude.core.tools.builtin.list_dir import ListDirTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool

# ── shell ─────────────────────────────────────────────────────────────────────

# 功能：验证 POSIX 平台通过 /bin/sh 执行命令而不依赖当前用户的交互 shell
# 设计：直接检查参数向量，避免测试环境实际 shell 配置影响跨平台选择逻辑
def test_shell_argv_uses_sh_on_posix() -> None:
    assert _shell_argv("echo hello", platform="posix") == ["/bin/sh", "-c", "echo hello"]


# 功能：验证 Windows 平台优先选择 PowerShell 并启用非交互模式
# 设计：替换 executable 查找结果以隔离宿主机安装情况，只断言稳定的启动参数
def test_shell_argv_uses_powershell_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kama_claude.core.tools.builtin.bash.shutil.which",
        lambda name: "pwsh.exe" if name == "pwsh" else None,
    )
    argv = _shell_argv("Write-Output hello", platform="nt")
    assert argv[0] == "pwsh.exe"
    assert "-NonInteractive" in argv
    assert argv[-1].endswith("Write-Output hello")


# 功能：验证公开工具名为 shell，且 Windows 描述明确约束模型使用 PowerShell 语法
# 设计：固定解释器探测为 Windows PowerShell，直接检查发给模型的名称和关键提示语
def test_shell_schema_describes_windows_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kama_claude.core.tools.builtin.bash.shutil.which",
        lambda name: "powershell.exe" if name == "powershell" else None,
    )
    assert ShellTool.name == "shell"
    description = _shell_description(platform="nt")
    assert "Windows PowerShell 5.1" in description
    assert "Use PowerShell syntax" in description
    assert "do not emit Bash/POSIX syntax" in description
    assert "does not support '&&' or '||'" in description

# 功能：验证成功命令的 stdout 出现在 ToolResult.content 中，is_error 为 False
# 设计：用 echo 命令避免外部依赖，直接比较输出内容，无需 mock
@pytest.mark.asyncio
async def test_shell_success_stdout() -> None:
    result = await ShellTool().invoke({"command": "echo hello"})
    assert not result.is_error
    assert "hello" in result.content


# 功能：验证设置工作目录后 shell 子进程继承该目录
# 设计：由 Python 子进程输出 os.getcwd，跨 Windows/POSIX 验证 cwd 注入而不依赖 shell 专属命令
@pytest.mark.asyncio
async def test_shell_uses_working_directory(tmp_path: Path) -> None:
    result = await ShellTool(tmp_path).invoke(
        {"command": 'python -c "import os; print(os.getcwd())"'}
    )

    assert not result.is_error
    assert str(tmp_path.resolve()).lower() in result.content.strip().lower()


# 功能：验证非零退出码时 is_error=True 且 content 包含退出码标注
# 设计：`exit 2` 是最简单的非零退出；不依赖任何外部命令行为
@pytest.mark.asyncio
async def test_shell_nonzero_exit_is_error() -> None:
    result = await ShellTool().invoke({"command": "exit 2"})
    assert result.is_error
    assert "[exit 2]" in result.content


# 功能：验证命令超时后 is_error=True，error_type 为 "timeout"
# 设计：timeout=1s 搭配 sleep 2 必然超时；验证 error_type 而非 content，避免超时消息格式耦合
@pytest.mark.asyncio
async def test_shell_timeout() -> None:
    result = await ShellTool().invoke({"command": "sleep 5", "timeout": 1})
    assert result.is_error
    assert result.error_type == "timeout"


# 功能：验证 stderr 被合并到 stdout 输出中
# 设计：只写 stderr 的命令（>&2 echo），输出应该出现在合并后的 content 里
@pytest.mark.asyncio
async def test_shell_stderr_merged() -> None:
    command = '[Console]::Error.WriteLine("err")' if os.name == "nt" else "echo err >&2"
    result = await ShellTool().invoke({"command": command})
    assert not result.is_error
    assert "err" in result.content


# ── write_file ────────────────────────────────────────────────────────────────

# 功能：验证 write_file 写入文件后内容可以被读取，返回字节数
# 设计：写入临时目录，断言文件存在且内容一致；用 tmp_path fixture 自动清理
@pytest.mark.asyncio
async def test_write_file_creates_and_returns_size(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    result = await WriteFileTool().invoke(
        {"path": str(target), "content": "hello world"}
    )
    assert not result.is_error
    assert "11" in result.content  # "hello world" = 11 bytes
    assert target.read_text() == "hello world"


# 功能：验证 write_file 和 list_dir 都从注入工作目录解析相对路径
# 设计：先相对写入再相对列目录，以一个闭环覆盖两个工具共享的工作区路径解析规则
@pytest.mark.asyncio
async def test_file_tools_use_working_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    written = await WriteFileTool(workspace).invoke(
        {"path": "src/new.py", "content": "value = 1"}
    )
    listed = await ListDirTool(workspace).invoke({"path": "src"})

    assert not written.is_error
    assert (workspace / "src" / "new.py").read_text() == "value = 1"
    assert "new.py" in listed.content


# 功能：验证 write_file 自动创建不存在的父目录
# 设计：路径包含两层不存在的子目录，确认写入后目录结构被创建
@pytest.mark.asyncio
async def test_write_file_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "file.txt"
    result = await WriteFileTool().invoke({"path": str(target), "content": "x"})
    assert not result.is_error
    assert target.exists()


# 功能：验证 write_file 拒绝包含 .. 的路径并抛出 PermissionError
# 设计：.. 路径遍历与 read_file 遵循相同规则，用相同的断言模式保持一致性
@pytest.mark.asyncio
async def test_write_file_rejects_traversal() -> None:
    with pytest.raises(PermissionError):
        await WriteFileTool().invoke({"path": "../secret.txt", "content": "x"})


# ── list_dir ──────────────────────────────────────────────────────────────────

# 功能：验证 list_dir 输出包含目录中的文件名
# 设计：在 tmp_path 创建已知结构，断言文件名出现在 content 中；不约束格式细节
@pytest.mark.asyncio
async def test_list_dir_shows_files(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("x")
    (tmp_path / "bar.md").write_text("y")
    result = await ListDirTool().invoke({"path": str(tmp_path)})
    assert not result.is_error
    assert "foo.py" in result.content
    assert "bar.md" in result.content


# 功能：验证 list_dir 按 max_depth 限制递归深度（depth=1 时不展示孙级目录内容）
# 设计：创建 parent/child/grandchild 三层，depth=1 时 grandchild 不应出现在输出中
@pytest.mark.asyncio
async def test_list_dir_respects_max_depth(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    grandchild = child / "grandchild"
    grandchild.mkdir()
    (grandchild / "deep.txt").write_text("x")

    result = await ListDirTool().invoke({"path": str(tmp_path), "max_depth": 1})
    assert not result.is_error
    assert "child" in result.content
    assert "deep.txt" not in result.content


# 功能：验证对不存在的路径 list_dir 抛出 FileNotFoundError
# 设计：直接传入不存在的路径字符串，预期抛出标准异常（invocation.py 捕获后返回 error ToolResult）
@pytest.mark.asyncio
async def test_list_dir_missing_path_raises() -> None:
    with pytest.raises(FileNotFoundError):
        await ListDirTool().invoke({"path": "/this/does/not/exist"})


# 功能：验证 list_dir 拒绝包含 .. 的路径
# 设计：与 read_file 和 write_file 保持一致的安全规则
@pytest.mark.asyncio
async def test_list_dir_rejects_traversal() -> None:
    with pytest.raises(PermissionError):
        await ListDirTool().invoke({"path": "../"})
