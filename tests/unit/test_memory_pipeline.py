from __future__ import annotations

from pathlib import Path

from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.types import LlmResponse
from agent_lite.core.memory.model import MemoryOperation
from agent_lite.core.memory.pipeline import (
    MemoryExtractor,
    MemoryPipeline,
    sanitize_transcript,
)
from agent_lite.core.memory.store import MemoryStore
from agent_lite.core.runner import RunOutcome
from agent_lite.core.session.manager import SessionManager
from agent_lite.core.session.model import Session
from agent_lite.core.session.store import SessionStore


class _Provider:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

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
        return LlmResponse(stop_reason="end_turn", text=self.responses.pop(0))


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


async def test_extractor_accepts_fenced_json_and_limits_temporary_items() -> None:
    provider = _Provider(
        """```json
        {"should_store": true, "summary": "stable preference", "items": [
          {"type":"preference","scope":"global","key":"response.style",
           "content":"Answer directly.","evidence":"user asked repeatedly",
           "confidence":0.9,"stability":"stable"},
          {"type":"fact","scope":"session","key":"session.note",
           "content":"Only for this task.","confidence":0.9,"stability":"temporary"}
        ]}
        ```"""
    )
    result = await MemoryExtractor(provider).extract(
        [{"role": "user", "content": "Please answer directly."}]
    )

    assert result.should_store
    assert len(result.items) == 1
    assert result.items[0].key == "response.style"


async def test_pipeline_persists_raw_items_and_consolidates_atomically(tmp_path: Path) -> None:
    provider = _Provider(
        '{"should_store":true,"summary":"preference","items":['
        '{"type":"preference","scope":"global","key":"response.style",'
        '"content":"Answer directly.","evidence":"explicit request",'
        '"confidence":0.95,"stability":"stable"}]}',
        '{"operations":[{"action":"add","target_key":"response.style",'
        '"type":"preference","scope":"global","content":"Answer directly.",'
        '"reason":"stable preference","confidence":0.95,"importance":0.8}]}',
    )
    store = MemoryStore(tmp_path / "memory.db")
    pipeline = MemoryPipeline(store, provider)

    count = await pipeline.process_session(
        session_id="session-1",
        run_id="run-1",
        transcript=[{"role": "user", "content": "Please answer directly."}],
        workspace_root=None,
    )

    assert count == 1
    memories = store.search("directly")
    assert len(memories) == 1
    raw = store.list_raw_items(session_id="session-1", status="processed")
    assert len(raw) == 1
    assert raw[0].run_id == "run-1"
    assert not await pipeline.process_session(
        session_id="session-1",
        run_id="run-2",
        transcript=[{"role": "user", "content": "another request"}],
        workspace_root=None,
    )
    assert len(provider.prompts) == 2


def test_phase2_rejects_sensitive_and_missing_update_without_writing(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    operations = [
        MemoryOperation(
            action="add",
            target_key="secret",
            type="fact",
            scope="global",
            content="token=do-not-store",
            confidence=1.0,
        ),
        MemoryOperation(
            action="update",
            target_key="missing",
            type="fact",
            scope="global",
            content="safe content",
            confidence=1.0,
        ),
    ]

    assert store.apply_memory_operations([], operations, session_id="s", workspace_root=None) == 0
    assert store.list_memories() == []


async def test_successful_session_run_schedules_background_pipeline(tmp_path: Path) -> None:
    provider = _Provider(
        '{"should_store":true,"items":[{"type":"preference","scope":"global",'
        '"key":"response.style","content":"Answer directly.","confidence":0.9,'
        '"stability":"stable"}]}',
        '{"operations":[{"action":"add","target_key":"response.style",'
        '"type":"preference","scope":"global","content":"Answer directly.",'
        '"confidence":0.9}]}',
    )

    class _Runner:
        async def run_and_capture(
            self,
            goal: str,
            *,
            run_id: str | None = None,
            session: Session | None = None,
            store: SessionStore | None = None,
            system_prompt_override: str | None = None,
            tool_whitelist: list[str] | None = None,
        ) -> RunOutcome:
            assert session is not None and store is not None and run_id is not None
            store.append_message(session.id, "assistant", "done")
            return RunOutcome(status="success", result="done", reason=None)

    memory = MemoryStore(tmp_path / "memory.db")
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: _Runner(),  # type: ignore[arg-type, return-value]
        EventBus(),
        provider=provider,
        memory_store=memory,
        memory_generate_enabled=True,
    )
    session = await manager.create("chat")

    await manager.send_message(session.id, "Please answer directly.")
    assert session.memory_generation_status != "idle"
    await manager.wait_for_memory_tasks()

    assert session.memory_generation_status == "succeeded"
    assert session.last_memory_extracted_run_id is not None
    assert memory.search("directly")
