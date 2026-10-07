from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
from pathlib import Path

from agent_lite.core.tools.working_directory import resolve_tool_path


# 优先使用显式路径，再查找独立安装位置和系统 PATH。
def ripgrep_path() -> str:
    configured = os.environ.get("AGENTLITE_RG")
    if configured:
        if not Path(configured).is_file():
            raise FileNotFoundError("AGENTLITE_RG must point to the ripgrep executable")
        return configured
    binary = "rg.exe" if os.name == "nt" else "rg"
    installed = Path.home() / ".agentlite" / "bin" / binary
    if installed.is_file():
        return str(installed)
    found = shutil.which("rg")
    if found:
        return found
    raise FileNotFoundError(
        "ripgrep is required. Install rg on PATH or set AGENTLITE_RG to its executable. "
        "Windows: winget install BurntSushi.ripgrep.MSVC"
    )


# 解析搜索根目录及目标，拒绝父目录跳转并支持单文件搜索。
def search_target(path: str, workspace: Path | None) -> tuple[Path, str]:
    if ".." in Path(path).parts:
        raise PermissionError(f"path traversal not allowed: {path}")
    target = resolve_tool_path(path, workspace).resolve()
    if not target.exists():
        raise FileNotFoundError(f"no such path: {path}")
    return (target, ".") if target.is_dir() else (target.parent, target.name)


# 以参数数组运行搜索并限制时间、输出大小和分页读取，不经过 shell。
async def run_rg(args: list[str], root: Path, records: int, separator: bytes) -> bytes:
    process = await asyncio.create_subprocess_exec(
        ripgrep_path(), "--no-config", "--color", "never", "--hidden",
        "--no-require-git", *args[:-2], "--glob", "!.git", "--glob", "!.git/**",
        *args[-2:],
        cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert process.stdout is not None and process.stderr is not None
    stderr_task = asyncio.create_task(process.stderr.read())
    output = bytearray()
    stopped = False
    try:
        async with asyncio.timeout(20):
            while chunk := await process.stdout.read(65536):
                output.extend(chunk)
                if len(output) > 16 * 1024 * 1024:
                    raise ValueError("Search output exceeds 16 MiB; narrow pattern or path")
                if output.count(separator) >= records:
                    stopped = True
                    break
            if stopped and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
            stderr = await stderr_task
            if not stopped and process.returncode not in (0, 1):
                raise ValueError(stderr.decode("utf-8", errors="replace").strip())
            if not stopped and stderr:
                raise ValueError(stderr.decode("utf-8", errors="replace").strip())
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        await process.wait()
        if not stderr_task.done():
            stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task
    return bytes(output)


# 从完整记录中提取分页并在实际截断时明确后续翻页方法。
def paged_text(data: bytes, separator: bytes, limit: int, offset: int) -> str:
    rows = [row.decode("utf-8", errors="replace") for row in data.split(separator) if row]
    if separator == b"\n":
        rows = [row.removesuffix("\r") for row in rows]
    rows = [row[2:] if row.startswith(("./", ".\\")) else row for row in rows]
    selected = rows[offset:offset + limit]
    result = "\n".join(selected) if selected else "No matches found."
    if len(rows) > offset + limit:
        result += (f"\n[Showing results with pagination = limit: {limit}, offset: {offset}. "
                   f"Use offset: {offset + limit} to continue.]")
    return result
