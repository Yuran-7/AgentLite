from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MemoryScope = Literal["global", "workspace", "session"]
MemoryType = Literal["preference", "fact", "decision", "procedure"]
MemoryStatus = Literal["active", "superseded", "deleted"]
CandidateAction = Literal["add", "update", "delete", "skip"]
RawMemoryStatus = Literal["pending", "processed", "failed"]


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


class RawMemoryItem(BaseModel):
    """A Phase 1 observation that has not yet been merged into active memory."""

    id: str
    session_id: str
    run_id: str
    type: MemoryType
    scope: MemoryScope
    key: str = ""
    content: str
    evidence: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    stability: Literal["stable", "temporary", "unknown"] = "unknown"
    summary: str = ""
    source_hash: str
    status: RawMemoryStatus = "pending"
    created_at: str
    processed_at: str | None = None


class MemoryExtractionItem(BaseModel):
    type: MemoryType
    scope: MemoryScope
    key: str = ""
    content: str
    evidence: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    stability: Literal["stable", "temporary", "unknown"] = "unknown"
    tags: list[str] = Field(default_factory=list)


class MemoryExtraction(BaseModel):
    """Strict JSON envelope returned by the Phase 1 extractor."""

    should_store: bool = False
    summary: str = ""
    items: list[MemoryExtractionItem] = Field(default_factory=list)


class MemoryOperation(BaseModel):
    """A proposed Phase 2 database operation; it has no write capability."""

    action: CandidateAction
    target_key: str = ""
    type: MemoryType | None = None
    scope: MemoryScope | None = None
    content: str = ""
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: str = ""
    tags: list[str] = Field(default_factory=list)


class MemoryConsolidation(BaseModel):
    """Strict JSON envelope returned by the Phase 2 consolidator."""

    operations: list[MemoryOperation] = Field(default_factory=list)
