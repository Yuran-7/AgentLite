from __future__ import annotations

from pathlib import Path

from agent_lite.core.memory.model import MemoryCandidate
from agent_lite.core.memory.store import MemoryStore, workspace_id


def test_candidate_is_pending_until_commit(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")

    candidates = store.generate_from_messages(
        [{"role": "user", "content": "remember that I prefer concise answers."}],
        session_id="sess-1",
        run_id="run-1",
        workspace_root=None,
    )

    assert len(candidates) == 1
    assert store.search("concise") == []
    record = store.commit_candidate(candidates[0].id)
    assert record is not None
    assert record.key == "user.preference"
    assert store.search("concise")[0].id == record.id


def test_reject_candidate_does_not_create_memory(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    candidate = store.generate_from_messages(
        [{"role": "user", "content": "remember that I like tea."}],
        session_id="sess-1",
        run_id="run-1",
        workspace_root=None,
    )[0]

    assert store.reject_candidate(candidate.id)
    assert store.get_candidate(candidate.id).status == "rejected"  # type: ignore[union-attr]
    assert store.search("tea") == []


def test_commit_supersedes_old_record_and_delete_keeps_tombstone(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    first = MemoryCandidate(
        id="candidate-1",
        action="add",
        scope="global",
        type="preference",
        key="response.style",
        content="Answer concisely.",
        confidence=0.9,
        importance=0.8,
        created_at="2026-01-01T00:00:00+00:00",
    )
    second = first.model_copy(
        update={
            "id": "candidate-2",
            "content": "Answer concisely and directly.",
        }
    )
    store.create_candidate(first)
    old = store.commit_candidate(first.id)
    assert old is not None
    store.create_candidate(second)
    new = store.commit_candidate(second.id)
    assert new is not None
    assert new.supersedes == old.id
    assert store.get(old.id) is not None
    assert store.get(old.id).status == "superseded"  # type: ignore[union-attr]

    assert store.delete(new.id)
    deleted = store.get(new.id)
    assert deleted is not None
    assert deleted.status == "deleted"
    assert store.search("concisely") == []


def test_search_only_returns_visible_scopes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    workspace = tmp_path / "repo"
    workspace.mkdir()

    records = [
        MemoryCandidate(
            id="global-candidate",
            action="add",
            scope="global",
            type="fact",
            key="user.fact",
            content="The user likes tea.",
            confidence=0.9,
            importance=0.5,
            created_at="2026-01-01T00:00:00+00:00",
        ),
        MemoryCandidate(
            id="workspace-candidate",
            action="add",
            scope="workspace",
            workspace_id=workspace_id(workspace),
            type="decision",
            key="project.database",
            content="This workspace uses SQLite.",
            confidence=0.9,
            importance=0.8,
            created_at="2026-01-01T00:00:00+00:00",
        ),
        MemoryCandidate(
            id="other-workspace-candidate",
            action="add",
            scope="workspace",
            workspace_id=workspace_id(tmp_path),
            type="decision",
            key="project.database",
            content="The other workspace uses Postgres.",
            confidence=0.9,
            importance=0.8,
            created_at="2026-01-01T00:00:00+00:00",
        ),
    ]
    for candidate in records:
        store.create_candidate(candidate)
        assert store.commit_candidate(candidate.id) is not None

    visible = store.search("database", workspace_root=workspace, session_id="sess")
    assert [record.content for record in visible] == ["This workspace uses SQLite."]
    assert store.search("tea", workspace_root=workspace, session_id="sess")[0].scope == "global"
