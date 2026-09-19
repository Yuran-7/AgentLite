from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_MARKER = ".agentlite-memory-workspace"
_DIFF_FILE = "phase2_workspace_diff.md"
_TRACKED_PATHS = ("MEMORY.md", "memory_summary.md", "rollout_summaries")
_MARKER_CONTENT = "AgentLite managed memory Git workspace.\n"


@dataclass(frozen=True)
class MemoryWorkspaceDiff:
    has_baseline: bool
    changes: tuple[str, ...]
    rendered: str

    # 判断相对上次成功基线是否存在变化。
    def has_changes(self) -> bool:
        return not self.has_baseline or bool(self.changes)


class MemoryGitWorkspace:
    """A disposable Git baseline for the memory artifact directory."""

    # 保存并验证由 AgentLite 管理的记忆根目录。
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError(f"memory root must not be a symlink: {self.root}")
        git_dir = self.root / ".git"
        marker = self.root / _MARKER
        if git_dir.exists() and (
            not marker.is_file()
            or marker.read_text(encoding="utf-8") != _MARKER_CONTENT
        ):
            raise ValueError(
                f"memory root contains an unmanaged Git repository: {self.root}"
            )

    # 返回供 consolidation agent 读取的 diff 文件路径。
    @property
    def diff_path(self) -> Path:
        return self.root / _DIFF_FILE

    # 使用参数数组调用 Git，禁止 shell 展开。
    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )

    # 判断目录是否包含可用且属于 AgentLite 的 Git baseline。
    def has_baseline(self) -> bool:
        marker = self.root / _MARKER
        if not marker.is_file() or marker.read_text(encoding="utf-8") != _MARKER_CONTENT:
            return False
        result = self._git("rev-parse", "--verify", "HEAD", check=False)
        return result.returncode == 0

    # 生成从上次成功 baseline 到当前记忆目录的有界 Git 风格变化说明。
    def diff(self, *, max_bytes: int = 100_000) -> MemoryWorkspaceDiff:
        self.remove_diff_file()
        if not self.has_baseline():
            rendered = (
                "# Memory Workspace Diff\n\n"
                "No successful Git baseline exists. Perform a full initialization from all "
                "rollout summaries.\n"
            )
            return MemoryWorkspaceDiff(False, ("INIT",), rendered)

        status_result = self._git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *_TRACKED_PATHS,
        )
        changes = tuple(line for line in status_result.stdout.splitlines() if line.strip())
        diff_result = self._git(
            "diff",
            "--no-ext-diff",
            "--unified=3",
            "HEAD",
            "--",
            *_TRACKED_PATHS,
        )
        unified_diff = diff_result.stdout
        encoded = unified_diff.encode("utf-8")
        if len(encoded) > max_bytes:
            unified_diff = encoded[:max_bytes].decode("utf-8", errors="ignore")
            unified_diff += f"\n[diff truncated at {max_bytes} bytes]\n"
        status = "\n".join(f"- `{line}`" for line in changes) or "- none"
        rendered = (
            "# Memory Workspace Diff\n\n"
            "Generated before Phase 2. Read this file first and do not edit it.\n\n"
            f"## Status\n\n{status}\n\n"
            f"## Diff\n\n```diff\n{unified_diff}```\n"
        )
        return MemoryWorkspaceDiff(True, changes, rendered)

    # 写入供受限 consolidation agent 阅读的 diff 文件。
    def write_diff_file(self, diff: MemoryWorkspaceDiff) -> Path:
        self.diff_path.write_text(diff.rendered, encoding="utf-8")
        return self.diff_path

    # 删除临时 diff 文件，避免它进入下一次变更检测。
    def remove_diff_file(self) -> None:
        try:
            self.diff_path.unlink()
        except FileNotFoundError:
            pass

    # 将当前已验证的记忆文件重建为不保留历史的单提交 baseline。
    def reset_baseline(self) -> None:
        git_dir = self.root / ".git"
        marker = self.root / _MARKER
        if git_dir.exists():
            if not marker.is_file() or marker.read_text(encoding="utf-8") != _MARKER_CONTENT:
                raise RuntimeError("refusing to replace an unmanaged Git repository")
            resolved_git = git_dir.resolve()
            if resolved_git.parent != self.root or git_dir.is_symlink():
                raise RuntimeError(f"unsafe memory Git directory: {git_dir}")
            shutil.rmtree(git_dir)

        self.remove_diff_file()
        marker.write_text(_MARKER_CONTENT, encoding="utf-8")
        (self.root / ".gitignore").write_text(
            "memory.db\nmemory.db-*\nphase2_workspace_diff.md\nagentlite-phase2-*\n",
            encoding="utf-8",
        )
        self._git("init", "--quiet")
        self._git("config", "user.name", "AgentLite Memory")
        self._git("config", "user.email", "memory@agentlite.local")
        baseline_paths = [_MARKER, ".gitignore"]
        baseline_paths.extend(
            path for path in _TRACKED_PATHS if (self.root / path).exists()
        )
        self._git("add", "-A", "--", *baseline_paths)
        self._git("commit", "--quiet", "--allow-empty", "-m", "memory baseline")
