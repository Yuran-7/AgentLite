from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.types import LlmResponse, ToolCallBlock
from agent_lite.core.memory.pipeline import MemoryExtractor, MemoryPipeline, sanitize_transcript
from agent_lite.core.memory.store import MemoryStore
from agent_lite.core.runner import RunOutcome
from agent_lite.core.session.manager import SessionManager
from agent_lite.core.session.model import Session
from agent_lite.core.session.store import SessionStore


class _Provider:
    # 保存按调用顺序返回的模型响应。
    def __init__(self, *responses: str | LlmResponse) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []

    # 返回预设响应并记录 Phase 1 prompt。
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
        self.prompts.append(str(messages[0]["content"]))
        self.system_prompts.append(system or "")
        response = self.responses.pop(0)
        return response if isinstance(response, LlmResponse) else LlmResponse(
            stop_reason="end_turn", text=response
        )


# 功能：验证会话清洗会移除系统消息、遮蔽凭据并保留工具结果。
# 设计：混合四种消息形态，直接断言传给摘要模型的最小安全表示。
def test_sanitize_transcript_removes_system_and_redacts_credentials() -> None:
    result = sanitize_transcript(
        [
            {"role": "system", "content": "internal instructions"},
            {"role": "user", "content": "Use token=secret-value and answer briefly."},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "shell", "input": {"x": 1}}],
            },
            {"role": "user", "content": [{"type": "tool_result", "content": "done"}]},
        ]
    )

    assert [item["role"] for item in result] == ["user", "assistant", "tool"]
    assert "secret-value" not in result[0]["content"]
    assert "[REDACTED]" in result[0]["content"]
    assert "internal instructions" not in str(result)


# 功能：验证 Phase 1 只接受单一 rollout_summary 字段并输出 Markdown。
# 设计：使用带 JSON 围栏的模型响应，覆盖常见但仍可安全解析的输出形式。
async def test_extractor_accepts_fenced_rollout_summary() -> None:
    provider = _Provider(
        '```json\n{"rollout_summary":"# Session summary\\n\\n## Task 1: answer style"}\n```'
    )

    result = await MemoryExtractor(provider).extract(
        [{"role": "user", "content": "Please answer directly."}]
    )

    assert result.rollout_summary.startswith("# Session summary")
    assert "raw_memory" not in provider.system_prompts[0]
    assert '"rollout_summary"' in provider.system_prompts[0]
    assert "Minimum-signal gate" in provider.system_prompts[0]
    assert "success|partial|fail|uncertain" in provider.system_prompts[0]
    assert "<transcript>" in provider.prompts[0]


# 功能：验证 Phase 1 写入稳定文件、保存三列元数据并按内容哈希去重。
# 设计：连续处理相同与变化后的 transcript，确认只更新同一 Session 文件。
async def test_pipeline_persists_one_summary_per_session(tmp_path: Path) -> None:
    provider = _Provider(
        '{"rollout_summary":"# First summary"}',
        '{"rollout_summary":"# Updated summary"}',
    )
    store = MemoryStore(tmp_path / "memory.db")
    pipeline = MemoryPipeline(store, provider)

    first = await pipeline.process_session(
        session_id="session-1",
        transcript=[{"role": "user", "content": "First request"}],
    )
    duplicate = await pipeline.process_session(
        session_id="session-1",
        transcript=[{"role": "user", "content": "First request"}],
    )
    updated = await pipeline.process_session(
        session_id="session-1",
        transcript=[{"role": "user", "content": "Changed request"}],
    )

    assert first and updated and not duplicate
    assert len(provider.prompts) == 2
    assert store.rollout_summary_path("session-1").read_text(encoding="utf-8") == (
        "# Updated summary\n"
    )
    metadata = store.get_rollout_summary_metadata("session-1")
    assert metadata is not None
    with sqlite3.connect(store.path) as connection:
        columns = connection.execute(
            "PRAGMA table_info(rollout_summary_sessions)"
        ).fetchall()
    assert [column[1] for column in columns] == ["session_id", "source_hash", "updated_at"]


# 功能：验证低信号会话的空摘要不会创建空 Markdown 文件。
# 设计：模型明确返回空字符串，同时确认哈希仍被记录以避免重复调用。
async def test_pipeline_noop_records_hash_without_creating_file(tmp_path: Path) -> None:
    provider = _Provider('{"rollout_summary":""}')
    store = MemoryStore(tmp_path / "memory.db")
    pipeline = MemoryPipeline(store, provider)
    transcript = [{"role": "user", "content": "hello"}]

    assert not await pipeline.process_session(session_id="session-1", transcript=transcript)
    assert not store.rollout_summary_path("session-1").exists()
    assert not await pipeline.process_session(session_id="session-1", transcript=transcript)
    assert len(provider.prompts) == 1


# 功能：验证新 Session 的第一条用户消息才触发一次历史摘要扫描。
# 设计：创建后先让出事件循环确认未扫描，再发送两条消息并确认模型只处理一个旧 Session。
async def test_first_user_message_schedules_one_historical_summary(tmp_path: Path) -> None:
    seen_memory_contexts: list[str] = []
    provider = _Provider(
        '{"rollout_summary":"# Completed task"}',
        LlmResponse(
            stop_reason="tool_use",
            tool_calls=[
                ToolCallBlock(
                    id="memory",
                    name="write_memory_artifact",
                    input={
                        "path": "MEMORY.md",
                        "content": (
                            "# Task Group: completed\n\n"
                            "scope: global\nkeywords: completed\n\n"
                            "## Source rollouts\n\n"
                            "- rollout_summaries/sess-20260919-120000-000000000000.md\n"
                        ),
                    },
                ),
                ToolCallBlock(
                    id="summary",
                    name="write_memory_artifact",
                    input={
                        "path": "memory_summary.md",
                        "content": "v1\n\n## Memory index\n\n- completed",
                    },
                ),
            ],
        ),
        LlmResponse(stop_reason="end_turn", text="done"),
    )

    class _Runner:
        # 模拟一次成功执行并追加 assistant 消息。
        async def run_and_capture(
            self,
            goal: str,
            *,
            run_id: str | None = None,
            session: Session | None = None,
            store: SessionStore | None = None,
            system_prompt_override: str | None = None,
            tool_whitelist: list[str] | None = None,
            memory_context: str = "",
        ) -> RunOutcome:
            assert session is not None and store is not None and run_id is not None
            seen_memory_contexts.append(memory_context)
            store.append_message(session.id, "assistant", "done")
            return RunOutcome(status="success", result="done", reason=None)

    session_store = SessionStore(tmp_path / "sessions")
    old_time = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    historical = Session(
        id="sess-20260919-120000-000000000000",
        mode="chat",
        status="waiting_for_input",
        title="historical",
        created_at=old_time,
        updated_at=old_time,
        last_chat_at=old_time,
        run_ids=["run-old"],
        memory_generate_enabled=True,
    )
    session_store.write_meta(historical)
    session_store.append_message(historical.id, "user", "Please answer directly.")
    session_store.append_message(historical.id, "assistant", "done")
    second_eligible_time = (datetime.now(UTC) - timedelta(hours=8)).isoformat()
    second_eligible = Session(
        id="sess-20260919-110000-000000000003",
        mode="chat",
        status="waiting_for_input",
        title="second eligible",
        created_at=second_eligible_time,
        updated_at=second_eligible_time,
        last_chat_at=second_eligible_time,
        run_ids=["run-second"],
        memory_generate_enabled=True,
    )
    session_store.write_meta(second_eligible)
    session_store.append_message(second_eligible.id, "user", "Summarize me later.")
    for session_id, age in (
        ("sess-20260919-130000-000000000001", timedelta(hours=1)),
        ("sess-20260901-120000-000000000002", timedelta(days=11)),
    ):
        timestamp = (datetime.now(UTC) - age).isoformat()
        outside_window = Session(
            id=session_id,
            mode="chat",
            status="waiting_for_input",
            title="outside window",
            created_at=timestamp,
            updated_at=timestamp,
            last_chat_at=timestamp,
            run_ids=["run-outside"],
            memory_generate_enabled=True,
        )
        session_store.write_meta(outside_window)
        session_store.append_message(outside_window.id, "user", "Do not summarize yet.")

    memory = MemoryStore(tmp_path / "memory.db")
    manager = SessionManager(
        session_store,
        lambda: _Runner(),  # type: ignore[arg-type, return-value]
        EventBus(),
        provider=provider,
        memory_store=memory,
        memory_generate_enabled=True,
    )

    current = await manager.create("chat")
    await asyncio.sleep(0)
    assert provider.prompts == []

    await manager.send_message(current.id, "Start the new session.")
    await manager.wait_for_memory_tasks()
    await manager.send_message(current.id, "Continue the same session.")
    await manager.wait_for_memory_tasks()

    persisted = session_store.read_meta(historical.id)
    assert persisted.memory_generation_status == "succeeded"
    assert persisted.last_memory_extracted_run_id == "run-old"
    assert memory.rollout_summary_path(historical.id).read_text(encoding="utf-8") == (
        "# Completed task\n"
    )
    assert not memory.rollout_summary_path("sess-20260919-130000-000000000001").exists()
    assert not memory.rollout_summary_path("sess-20260901-120000-000000000002").exists()
    assert not memory.rollout_summary_path(second_eligible.id).exists()
    assert len(provider.prompts) == 3
    assert seen_memory_contexts[0] == ""
    assert "## Long-term memory" in seen_memory_contexts[1]
    assert "completed" in seen_memory_contexts[1]
