from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from agent_lite.core.tools.builtin.bash import _shell_argv
from agent_lite.core.tools.working_directory import resolve_tool_path

QUERY_TOOLS = frozenset(
    {
        "read_file",
        "list_dir",
        "web_search",
        "web_fetch",
        "update_plan",
        "request_user_input",
        "cosil_localize",
    }
)
EDIT_TOOLS = frozenset({"write_file", "edit_file"})
_PROTECTED_DIRS = {".git", ".agentlite", ".claude", ".codex", ".agents", ".vscode"}
_PROFILES = {".bashrc", ".zshrc", ".profile", ".bash_profile"}
_META = re.compile(r"[;&|<>`$\r\n(){}*?\[\]\x00]")
_TOKENS = re.compile(r""""[^"\r\n]*"|'[^'\r\n]*'|[^\s'"]+""")
_DANGER = re.compile(
    r"(?:^|[\s;&|])(sudo|su|doas|runas|mkfs(?:\.\w+)?|format|crontab|"
    r"shutdown|invoke-expression|iex|set-executionpolicy)(?:\s|$)|"
    r"\b(?:rm|remove-item|ri|del|erase|rd|rmdir)\b"
    r"[^\r\n;&|]*\s-(?:[a-z]*r[a-z]*|recurse)\b|"
    r"\b(?:rd|rmdir|del|erase)\b[^\r\n;&|]*/s\b|"
    r"\bdd\s+[^\r\n]*\bif=|\bchmod\s+[^\r\n]*(?:777|-[a-z]*r[a-z]*)\b|"
    r"\bicacls\b[^\r\n]*/grant\s+[\"']?Everyone\b|"
    r"\bschtasks\b[^\r\n]*/create\b|\breg\s+(?:add|delete)\b|"
    r"\bnetsh\s+advfirewall\b|"
    r"\bgit\b[^\r\n;&|]*\s(?:reset\b[^\r\n]*--hard|"
    r"clean\b[^\r\n]*\s-[a-z]*f[a-z]*\b|"
    r"push\b[^\r\n]*(?:--force(?:-with-lease)?\b|\s-[a-z]*f[a-z]*\b))|"
    r"\b(?:curl|wget|irm|iwr|invoke-restmethod|invoke-webrequest)\b"
    r"[^\r\n]*\|\s*(?:sh|bash|iex)\b",
    re.IGNORECASE,
)


# 获取与实际工具执行一致的 shell 类型。
def shell_kind() -> str:
    executable = Path(_shell_argv("")[0]).name.lower()
    return (
        "powershell"
        if executable in {"pwsh", "pwsh.exe", "powershell", "powershell.exe"}
        else ("cmd" if os.name == "nt" else "posix")
    )


# 比较解析后的路径包含关系，兼容 Windows 大小写且不使用字符串前缀。
def within(path: Path, root: Path) -> bool:
    target = Path(os.path.normcase(str(path.resolve())))
    base = Path(os.path.normcase(str(root.resolve())))
    return target.is_relative_to(base)


# 同时检测路径自身和链接解析后的受保护目录与配置文件。
def protected_path(path: Path, workspace: Path | None) -> bool:
    for candidate in (path.absolute(), path.resolve()):
        parts = [part.lower() for part in candidate.parts]
        if any(part in _PROTECTED_DIRS or part.startswith(".env") for part in parts):
            return True
        name = candidate.name.lower()
        if name in _PROFILES or name.endswith("profile.ps1"):
            return True
        if within(candidate, Path.home() / ".ssh"):
            return True
    return False


# 按文件工具的同一解析方式获得目标，不可靠路径保守退出快速通道。
def edit_target(params: dict[str, Any], workspace: Path | None) -> Path | None:
    raw = params.get("path")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    try:
        return resolve_tool_path(raw, workspace).absolute()
    except (OSError, ValueError, RuntimeError):
        return None


# 只拆分单条字面量命令；动态表达式、逃逸语法及引号错误返回未知。
def literal_tokens(command: str, kind: str) -> list[str] | None:
    if _META.search(command) or (kind == "cmd" and re.search(r"[%!^]", command)):
        return None
    matches = list(_TOKENS.finditer(command))
    offset = 0
    tokens: list[str] = []
    for match in matches:
        if command[offset : match.start()].strip():
            return None
        token = match.group()
        if kind == "posix" and "\\" in token:
            return None
        tokens.append(token[1:-1] if token[:1] in {"'", '"'} else token)
        offset = match.end()
    return tokens if not command[offset:].strip() and tokens else None


# 按命令和受限参数判定只读，未知选项和工作区外路径一律返回未知。
def readonly_shell(command: str, workspace: Path | None, kind: str) -> bool:
    tokens = literal_tokens(command, kind)
    if not tokens or workspace is None:
        return False
    name, *args = tokens
    name = name.lower()
    if name in {"pwd", "get-location"}:
        return not args
    if name == "git":
        if not args:
            return False
        sub, *flags = args
        permitted = {
            "status": {"--short", "--porcelain", "--branch", "-s", "-b"},
            "branch": {"--list", "--all", "--remotes", "-a", "-r", "-v", "-vv"},
            "remote": {"-v", "--verbose"},
        }
        return (
            sub in permitted
            and all(flag in permitted[sub] for flag in flags)
            and (sub != "remote" or bool(flags))
        )
    if name not in {"ls", "dir", "cat", "type", "get-childitem", "get-content", "rg"}:
        return False
    options = {
        "ls": {"-l", "-a", "-la", "-al"},
        "dir": set(),
        "cat": {"-n"},
        "type": set(),
        "get-childitem": {"-force", "-recurse", "-name", "-path", "-literalpath"},
        "get-content": {"-raw", "-path", "-literalpath"},
        "rg": {"--files", "--hidden", "-n", "-i", "--fixed-strings", "-F", "--"},
    }
    operands: list[str] = []
    for arg in args:
        if arg.startswith("-"):
            if arg not in options[name] and arg.lower() not in options[name]:
                return False
        else:
            operands.append(arg)
    if name == "rg" and "--files" not in args:
        if not operands:
            return False
        operands = operands[1:]
    try:
        return all(within(resolve_tool_path(arg, workspace), workspace) for arg in operands)
    except (OSError, ValueError, RuntimeError):
        return False


# 检测明确高危操作，避免分类器以用户意图覆盖强制审批。
def dangerous_shell(command: str) -> bool:
    normalized = re.sub(r"[\"'`^]", "", command)
    return bool(_DANGER.search(command) or _DANGER.search(normalized))


# 对可识别的受保护路径访问保守要求审批，已确认只读的命令除外。
def protected_shell(command: str, workspace: Path | None, kind: str) -> bool:
    if readonly_shell(command, workspace, kind):
        return False
    return bool(
        re.search(
            r"(?:^|[\s/\\\"'])\.(?:git|agentlite|claude|codex|agents|vscode|ssh)(?:[/\\\s\"']|$)|"
            r"(?:^|[\s/\\\"'])\.env[^\s]*|\.(?:bashrc|zshrc|profile|bash_profile)\b|"
            r"[\w.-]*profile\.ps1\b",
            command,
            re.IGNORECASE,
        )
    )
