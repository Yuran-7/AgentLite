from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.types import LlmResponse, ToolCallBlock
from agent_lite.core.memory.phase2 import MemoryConsolidator
from agent_lite.core.memory.store import MemoryStore
from agent_lite.core.memory.tools import MemoryReadTool, MemoryWriteTool


class _Provider:
    # 保存 AgentLoop 将按顺序消费的响应。
    def __init__(self, *responses: LlmResponse) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.tool_names: set[str] = set()
        self.system_prompts: list[str] = []

    # 返回预设的工具调用或结束响应。
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self.calls += 1
        self.tool_names.update(str(schema["name"]) for schema in tool_schemas)
        self.system_prompts.append(system or "")
        return self.responses.pop(0)


# 构造一次写入两个 Phase 2 产物并结束的 Agent 响应序列。
def _successful_responses(memory: str, summary: str) -> tuple[LlmResponse, LlmResponse]:
    return (
        LlmResponse(
            stop_reason="tool_use",
            tool_calls=[
                ToolCallBlock(
                    id="write-memory",
                    name="write_memory_artifact",
                    input={"path": "MEMORY.md", "content": memory},
                ),
                ToolCallBlock(
                    id="write-summary",
                    name="write_memory_artifact",
                    input={"path": "memory_summary.md", "content": summary},
                ),
            ],
        ),
        LlmResponse(stop_reason="end_turn", text="Consolidation complete."),
    )


# 功能：验证首次 consolidation 生成两个文件、建立 baseline，随后无变化时不调用模型。
# 设计：使用真实 Git workspace 和工具调用 stub，覆盖 INIT、发布、校验及 no-change gate。
@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
async def test_phase2_initializes_and_skips_unchanged_workspace(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    rollouts = root / "rollout_summaries"
    rollouts.mkdir(parents=True)
    (rollouts / "session-1.md").write_text("# Useful rollout\n", encoding="utf-8")
    memory = (
        "# Task Group: memory\n\n"
        "scope: global\nkeywords: memory\n\n"
        "## Source rollouts\n\n- rollout_summaries/session-1.md\n"
    )
    provider = _Provider(*_successful_responses(memory, "v1\n\n## Memory index\n\n- memory"))
    consolidator = MemoryConsolidator(root, provider)

    assert await consolidator.consolidate_if_needed()
    assert (root / "MEMORY.md").read_text(encoding="utf-8") == memory
    assert (root / "memory_summary.md").read_text(encoding="utf-8").startswith("v1\n")
    assert (root / ".git").is_dir()
    assert not (root / "phase2_workspace_diff.md").exists()
    assert provider.tool_names == {
        "list_memory_files",
        "read_memory_file",
        "write_memory_artifact",
    }
    assert "raw_memories.md" not in provider.system_prompts[0]

    assert not await consolidator.consolidate_if_needed()
    assert provider.calls == 2


# 功能：验证没有 rollout、旧产物和 baseline 时 Phase 2 直接 no-op。
# 设计：使用没有预设响应的 provider，若错误调用模型测试会立即失败。
async def test_phase2_empty_workspace_is_noop(tmp_path: Path) -> None:
    provider = _Provider()

    assert not await MemoryConsolidator(tmp_path / "memory", provider).consolidate_if_needed()
    assert provider.calls == 0


# 功能：验证 Phase 2 不会占用或删除用户已有的非托管 Git 仓库。
# 设计：构造没有 AgentLite marker 的 .git 目录并在初始化阶段要求明确拒绝。
def test_phase2_rejects_unmanaged_git_repository(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    (root / ".git").mkdir(parents=True)

    with pytest.raises(ValueError, match="unmanaged Git repository"):
        MemoryConsolidator(root, _Provider())


# 功能：验证 Phase 2 校验失败时不发布半成品，也不推进 Git baseline。
# 设计：先建立成功 baseline，再修改 rollout 并返回错误版本头，检查旧产物保留且可重试。
@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
async def test_phase2_failure_preserves_artifacts_and_dirty_baseline(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    rollouts = root / "rollout_summaries"
    rollouts.mkdir(parents=True)
    rollout = rollouts / "session-1.md"
    rollout.write_text("# First\n", encoding="utf-8")
    old_memory = "# Task Group: first\n"
    initial = _Provider(*_successful_responses(old_memory, "v1\n\n## Memory index\n"))
    assert await MemoryConsolidator(root, initial).consolidate_if_needed()

    rollout.write_text("# Changed\n", encoding="utf-8")
    invalid = _Provider(*_successful_responses("# Broken replacement", "v2\ninvalid"))
    with pytest.raises(ValueError, match="must start"):
        await MemoryConsolidator(root, invalid).consolidate_if_needed()

    assert (root / "MEMORY.md").read_text(encoding="utf-8") == old_memory
    assert (root / "memory_summary.md").read_text(encoding="utf-8").startswith("v1\n")
    assert MemoryConsolidator(root, _Provider())._workspace.diff().has_changes()


# 功能：验证 consolidation 写工具不能修改 rollout、使用绝对路径或路径穿越。
# 设计：直接调用受限工具，以权限错误证明 staging 根目录与目标文件白名单生效。
async def test_phase2_tools_are_confined_to_staging(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    write = MemoryWriteTool(staging)
    read = MemoryReadTool(staging)

    with pytest.raises(PermissionError):
        await write.invoke({"path": "rollout_summaries/a.md", "content": "changed"})
    with pytest.raises(PermissionError):
        await read.invoke({"path": "../outside.md"})
    with pytest.raises(PermissionError):
        await read.invoke({"path": str((tmp_path / "outside.md").resolve())})


# 功能：验证只有合法 v1 memory_summary 才会进入 system prompt 上下文。
# 设计：依次写入缺失、错误版本和正确版本，检查 loader 的拒绝及长度边界。
def test_memory_summary_prompt_context_requires_v1(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    summary = tmp_path / "memory_summary.md"

    assert store.format_memory_summary() == ""
    summary.write_text("v2\nold", encoding="utf-8")
    assert store.format_memory_summary() == ""
    summary.write_text("v1\n\n## Memory index\n\n- useful topic", encoding="utf-8")

    context = store.format_memory_summary(max_chars=20)
    assert context.startswith("## Long-term memory")
    assert "v1" in context
    assert "fallible historical context" in context
