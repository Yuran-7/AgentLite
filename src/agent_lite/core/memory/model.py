from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MemoryScope = Literal["global", "workspace", "session"]
MemoryType = Literal["preference", "fact", "decision", "procedure"]
MemoryStatus = Literal["active", "superseded", "deleted"]
CandidateAction = Literal["add", "update", "delete", "skip"]


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


class MemoryCandidate(BaseModel):
    id: str
    action: CandidateAction
    scope: MemoryScope
    profile_id: str = "default"
    workspace_id: str | None = None
    session_id: str | None = None
    type: MemoryType
    key: str
    content: str
    reason: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    importance: float = Field(ge=0.0, le=1.0)
    evidence: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    status: Literal["pending", "accepted", "rejected"] = "pending"
    created_at: str

