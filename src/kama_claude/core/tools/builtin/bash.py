from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import BaseTool, ToolResult

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB
_DEFAULT_TIMEOUT = 60


# 根据当前平台构造非交互 shell 命令，Windows 优先 PowerShell，POSIX 使用 /bin/sh
def _shell_argv(command: str, platform: str | None = None) -> list[str]:
    current_platform = platform or os.name
    if current_platform == "nt":
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is not None:
            utf8_command = (
                "$OutputEncoding = [Console]::OutputEncoding = "
                "[System.Text.UTF8Encoding]::new(); "
                + command
            )
            return [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                utf8_command,
            ]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    return ["/bin/sh", "-c", command]


# 根据当前平台和可用解释器生成提供给模型的精确 shell 说明
def _shell_description(platform: str | None = None) -> str:
    current_platform = platform or os.name
    common = (
        "Execute one non-interactive platform-native shell command and return stdout and "
        "stderr combined. Prefer short, focused commands. Commands requiring user input "
        "will time out, and output is truncated at 64 KB. "
    )
    if current_platform == "nt":
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is not None:
            executable = os.path.basename(powershell).lower()
            runtime = (
                "PowerShell 7+ (pwsh)"
                if executable.startswith("pwsh")
                else "Windows PowerShell 5.1 (powershell.exe)"
            )
            legacy_syntax = (
                " Windows PowerShell 5.1 does not support '&&' or '||'; use ';' or "
                "PowerShell control flow instead."
                if not executable.startswith("pwsh")
                else ""
            )
            return (
                common
                + f"This Windows runtime executes commands with {runtime}. Use PowerShell "
                "syntax, including $env:NAME for environment variables; do not emit Bash/POSIX "
                "syntax such as 'VAR=value command', 'export', 'source', or '[ -f path ]'."
                + legacy_syntax
            )
        return (
            common
            + "This Windows runtime executes commands with cmd.exe. Use Command Prompt syntax, "
            "including %NAME% for environment variables; do not emit Bash/POSIX syntax."
        )
    return (
        common
        + "This runtime executes commands with POSIX /bin/sh, not necessarily Bash. Use portable "
        "POSIX shell syntax and avoid Bash-only constructs."
    )


class ShellParams(BaseModel):
    model_config = ConfigDict(extra="ignore") # 这个是BaseModel里面的一个类变量，不校验
    
    command: str
    timeout: int = Field(default=_DEFAULT_TIMEOUT, ge=1, le=120)


class ShellTool(BaseTool):
    params_model = ShellParams # python中称为类变量，有点像c++中的static成员变量
    name = "shell"
    description = _shell_description()
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum seconds to wait (default {_DEFAULT_TIMEOUT}, max 120).",
            },
        },
        "required": ["command"],
    }

    # 初始化可选工作目录，未设置时继续使用进程 cwd
    def __init__(self, working_directory: Path | None = None) -> None:
        self._working_directory = working_directory

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ShellParams.model_validate(params)
        command = p.command
        timeout = p.timeout

        try:
            proc = await asyncio.create_subprocess_exec(
                *_shell_argv(command),
                cwd=self._working_directory,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout_bytes, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolResult(
                    content=f"[timeout after {timeout}s]",
                    is_error=True,
                    error_type="timeout",
                )
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

        output = stdout_bytes.decode("utf-8", errors="replace")
        truncated = len(stdout_bytes) > _MAX_OUTPUT_BYTES
        if truncated:
            output = output[:_MAX_OUTPUT_BYTES] + "\n[truncated]"

        returncode = proc.returncode or 0
        if returncode != 0:
            return ToolResult(
                content=f"[exit {returncode}]\n{output}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=output or "[no output]")


# 保留旧 Python 导入名称，避免第三方扩展升级时立即失效；对模型只暴露 shell
BashParams = ShellParams
BashTool = ShellTool
