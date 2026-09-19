from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MemoryScope = Literal["global", "workspace", "session"]
MemoryType = Literal["preference", "fact", "decision", "procedure"]
MemoryStatus = Literal["active", "superseded", "deleted"]


class MemoryRecord(BaseModel):
    id: str
    scope: MemoryScope
    profile_id: str = "default"
    workspace_id: str | None = None
    session_id: str | None = None
    type: MemoryType
    key: str
    content: str
    status: MemoryStatus = "active"
    confidence: float = Field(ge=0.0, le=1.0)
    importance: float = Field(ge=0.0, le=1.0)
    source: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    expires_at: str | None = None
    supersedes: str | None = None
    deleted_at: str | None = None
    reason: str | None = None


class RolloutSummary(BaseModel):
    """Strict JSON envelope returned by the Phase 1 summarizer."""

    model_config = ConfigDict(extra="forbid")

    rollout_summary: str = ""


class RolloutSummaryMetadata(BaseModel):
    """Minimal SQLite metadata for one session-level rollout summary."""

    session_id: str
    source_hash: str
    updated_at: str
