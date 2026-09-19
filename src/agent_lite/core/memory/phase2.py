from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import uuid
from importlib.resources import files
from pathlib import Path

from agent_lite.core.context import ExecutionContext
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.base import LLMProvider
from agent_lite.core.loop import AgentLoop
from agent_lite.core.memory.git_workspace import MemoryGitWorkspace
from agent_lite.core.memory.tools import MemoryListTool, MemoryReadTool, MemoryWriteTool
from agent_lite.core.tools.registry import ToolRegistry

_PHASE2_PROMPT = (
    files("agent_lite.core.memory")
    .joinpath("phase2_prompt.md")
    .read_text(encoding="utf-8")
    .strip()
)
_ROLLOUT_REFERENCE_RE = re.compile(r"rollout_summaries/[A-Za-z0-9._-]+\.md")


class MemoryConsolidator:
    """Git-gated Phase 2 consolidation with an isolated tool-capable agent."""

    # 保存记忆目录、模型和产物长度限制。
    def __init__(
        self,
        root: Path,
        provider: LLMProvider,
        *,
        summary_max_chars: int = 10_000,
        max_steps: int = 12,
    ) -> None:
        self._root = root.expanduser().resolve()
        self._provider = provider
        self._summary_max_chars = max(1_000, summary_max_chars)
        self._max_steps = max(4, max_steps)
        self._workspace = MemoryGitWorkspace(self._root)
        self._lock = asyncio.Lock()

    # 在 Git workspace 有变化或全局产物无效时运行一次 consolidation。
    async def consolidate_if_needed(self) -> bool:
        async with self._lock:
            rollout_dir = self._root / "rollout_summaries"
            has_rollouts = rollout_dir.is_dir() and any(rollout_dir.glob("*.md"))
            has_artifacts = any(
                (self._root / name).exists()
                for name in ("MEMORY.md", "memory_summary.md")
            )
            if not self._workspace.has_baseline() and not has_rollouts and not has_artifacts:
                return False
            workspace_diff = self._workspace.diff()
            artifacts_valid = self._artifacts_valid(self._root)
            if not workspace_diff.has_changes() and artifacts_valid:
                return False

            self._workspace.write_diff_file(workspace_diff)
            try:
                memory, summary = await self._run_staged_agent()
                self._publish(memory, summary)
                self._validate_artifacts(self._root)
                self._workspace.reset_baseline()
                return True
            finally:
                self._workspace.remove_diff_file()

    # 在隔离 staging 目录运行只具备记忆读写工具的 AgentLoop。
    async def _run_staged_agent(self) -> tuple[str, str]:
        with tempfile.TemporaryDirectory(prefix="agentlite-phase2-", dir=self._root) as raw_dir:
            staging = Path(raw_dir)
            rollout_source = self._root / "rollout_summaries"
            if rollout_source.is_dir():
                rollout_staging = staging / "rollout_summaries"
                rollout_staging.mkdir()
                for source in rollout_source.glob("*.md"):
                    if source.is_symlink():
                        raise ValueError(f"rollout summary must not be a symlink: {source}")
                    shutil.copy2(source, rollout_staging / source.name)
            for name in ("MEMORY.md", "memory_summary.md", "phase2_workspace_diff.md"):
                source = self._root / name
                if source.is_file():
                    shutil.copy2(source, staging / name)

            registry = ToolRegistry()
            registry.register(MemoryListTool(staging))
            registry.register(MemoryReadTool(staging))
            registry.register(MemoryWriteTool(staging))
            context = ExecutionContext(
                run_id=f"memory-phase2-{uuid.uuid4().hex}",
                goal=(
                    "Consolidate the changed rollout summaries into MEMORY.md and "
                    "memory_summary.md. Follow the Phase 2 system instructions exactly."
                ),
                max_steps=self._max_steps,
                workspace_root=staging,
                system_prompt_override=_PHASE2_PROMPT,
            )
            try:
                await AgentLoop(self._provider, registry, EventBus()).run(context)
            finally:
                await registry.aclose()
            if context.status != "success":
                raise RuntimeError(f"memory Phase 2 agent failed: {context.reason}")
            self._validate_artifacts(staging)
            return (
                (staging / "MEMORY.md").read_text(encoding="utf-8"),
                (staging / "memory_summary.md").read_text(encoding="utf-8"),
            )

    # 返回两个正式产物是否都满足最低格式要求。
    def _artifacts_valid(self, root: Path) -> bool:
        try:
            self._validate_artifacts(root)
        except (OSError, ValueError):
            return False
        return True

    # 校验产物格式、summary 长度和 rollout 引用完整性。
    def _validate_artifacts(self, root: Path) -> None:
        memory_path = root / "MEMORY.md"
        summary_path = root / "memory_summary.md"
        memory = memory_path.read_text(encoding="utf-8").strip()
        summary = summary_path.read_text(encoding="utf-8").strip()
        if not memory:
            raise ValueError("MEMORY.md is empty")
        if summary.splitlines()[:1] != ["v1"]:
            raise ValueError("memory_summary.md must start with exact line 'v1'")
        if len(summary) > self._summary_max_chars:
            raise ValueError(
                f"memory_summary.md exceeds {self._summary_max_chars} characters"
            )
        for reference in _ROLLOUT_REFERENCE_RE.findall(memory):
            if not (root / reference).is_file():
                raise ValueError(f"MEMORY.md references missing rollout summary: {reference}")

    # 原子发布两个已验证文件，并在第二次替换失败时恢复旧版本。
    def _publish(self, memory: str, summary: str) -> None:
        targets = {
            self._root / "MEMORY.md": memory.rstrip() + "\n",
            self._root / "memory_summary.md": summary.rstrip() + "\n",
        }
        previous = {
            path: path.read_bytes() if path.is_file() else None for path in targets
        }
        temporary: dict[Path, Path] = {}
        try:
            for path, content in targets.items():
                temp = path.with_suffix(path.suffix + ".tmp")
                temp.write_text(content, encoding="utf-8")
                temporary[path] = temp
            for path, temp in temporary.items():
                temp.replace(path)
        except Exception:
            for path, old_content in previous.items():
                if old_content is None:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    path.write_bytes(old_content)
            raise
        finally:
            for temp in temporary.values():
                try:
                    temp.unlink()
                except FileNotFoundError:
                    pass
