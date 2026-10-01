from __future__ import annotations

import sys
from pathlib import Path

import pytest

import agent_lite.core.app  # noqa: F401 通过实际应用入口初始化包，避免包级导入循环
from agent_lite.core.mcp.client import McpClient
from agent_lite.core.tools.builtin.bash import ShellTool

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(sys.platform != "win32", reason="Windows only")]


# 功能：MCP stdio 服务可以正常握手且没有控制台窗口
# 设计：真实 Python 子进程报告自己的 Windows 控制台句柄，避免只验证 mock 参数
async def test_mcp_server_has_no_console(tmp_path: Path) -> None:
    script = tmp_path / "mcp_fixture.py"
    script.write_text(
        "import ctypes, json, sys\n"
        "ctypes.windll.kernel32.GetConsoleWindow.restype = ctypes.c_void_p\n"
        "ctypes.windll.user32.IsWindowVisible.argtypes = [ctypes.c_void_p]\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if 'id' not in request: continue\n"
        "    window = ctypes.windll.kernel32.GetConsoleWindow()\n"
        "    result = {'console': bool(ctypes.windll.user32.IsWindowVisible(window)) if window else False}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    client = McpClient()
    try:
        await client.connect_stdio(sys.executable, [str(script)])
        result = await client._call("console", {})
        assert result == {"console": False}
    finally:
        await client.close()


# 功能：shell 工具命令正常返回输出且不会分配可见控制台
# 设计：通过实际 PowerShell 与 Python 子进程检测句柄，覆盖工具执行路径
async def test_shell_command_has_no_console(tmp_path: Path) -> None:
    script = tmp_path / "console_fixture.py"
    script.write_text(
        "import ctypes\n"
        "ctypes.windll.kernel32.GetConsoleWindow.restype = ctypes.c_void_p\n"
        "ctypes.windll.user32.IsWindowVisible.argtypes = [ctypes.c_void_p]\n"
        "window = ctypes.windll.kernel32.GetConsoleWindow()\n"
        "print(bool(ctypes.windll.user32.IsWindowVisible(window)) if window else False)\n",
        encoding="utf-8",
    )
    result = await ShellTool(tmp_path).invoke({"command": f'& "{sys.executable}" "{script}"'})
    assert not result.is_error, result.content
    assert result.content.strip() == "False"
