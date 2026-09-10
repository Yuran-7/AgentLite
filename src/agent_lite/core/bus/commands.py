from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field

from agent_lite.core.memory.model import MemoryRecord, MemoryScope, RawMemoryItem
from agent_lite.core.session.model import SessionMode, SessionStatus


class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str


class PongResult(BaseModel):
    server_version: str
    uptime_ms: int
    received_at: str  # ISO 8601


class CoreShutdownCommand(BaseModel):
    type: Literal["core.shutdown"] = "core.shutdown"


class CoreShutdownResult(BaseModel):
    accepted: bool = True


class AgentRunCommand(BaseModel):
    type: Literal["agent.run"] = "agent.run"
    goal: str
    workspace_root: str | None = None


class AgentRunResult(BaseModel):
    run_id: str


class EventSubscribeCommand(BaseModel):
    type: Literal["event.subscribe"] = "event.subscribe"
    topics: list[str]          # fnmatch 模式，如 ["step.*", "tool.*"]
    scope: str = "global"      # "global" | "run:<run_id>" | "session:<session_id>"
    replay_from_run: str | None = None  # 设置则先从 events.jsonl 回放历史再接实时流


class EventSubscribeResult(BaseModel):
    subscription_id: str
    replayed_count: int = 0


class SessionCreateCommand(BaseModel):
    type: Literal["session.create"] = "session.create"
    mode: SessionMode = "chat"
    title: str = ""
    workspace_root: str | None = None


class SessionCreateResult(BaseModel):
    session_id: str
    status: SessionStatus
    workspace_root: str | None = None
    memory_generate_enabled: bool = False
    memory_use_enabled: bool = True


class SessionSummary(BaseModel):
    session_id: str
    title: str
    status: SessionStatus
    workspace_root: str | None = None
    created_at: str
    updated_at: str


class SessionListCommand(BaseModel):
    type: Literal["session.list"] = "session.list"
    workspace_root: str | None = None


class SessionListResult(BaseModel):
    sessions: list[SessionSummary]


class SessionResumeCommand(BaseModel):
    type: Literal["session.resume"] = "session.resume"
    session_id: str
    workspace_root: str | None = None


class SessionResumeResult(BaseModel):
    session_id: str
    title: str
    status: SessionStatus
    workspace_root: str | None = None
    memory_generate_enabled: bool = False
    memory_use_enabled: bool = True
    stats: dict[str, Any] = Field(default_factory=dict)


class SessionSetWorkspaceCommand(BaseModel):
    type: Literal["session.set_workspace"] = "session.set_workspace"
    session_id: str
    workspace_root: str


class SessionSetWorkspaceResult(BaseModel):
    workspace_root: str


class SessionSendMessageCommand(BaseModel):
    type: Literal["session.send_message"] = "session.send_message"
    session_id: str
    content: str


class SessionSendMessageResult(BaseModel):
    run_id: str


class SessionGetHistoryCommand(BaseModel):
    type: Literal["session.get_history"] = "session.get_history"
    session_id: str


class SessionGetHistoryResult(BaseModel):
    messages: list[dict[str, Any]]


class SessionCloseCommand(BaseModel):
    type: Literal["session.close"] = "session.close"
    session_id: str


class SessionCloseResult(BaseModel):
    status: SessionStatus


class PermissionRespondCommand(BaseModel):
    type: Literal["permission.respond"] = "permission.respond"
    tool_use_id: str
    # "allow_once" | "always_allow" | "deny_once" | "always_deny"
    decision: str


class PermissionRespondResult(BaseModel):
    ok: bool = True


class SessionCompactCommand(BaseModel):
    type: Literal["session.compact"] = "session.compact"
    session_id: str
    focus: str = ""


class SessionCompactResult(BaseModel):
    summary_tokens: int
    saved_tokens: int


class SessionSetMemoryCommand(BaseModel):
    type: Literal["session.set_memory"] = "session.set_memory"
    session_id: str
    generate_enabled: bool | None = None
    use_enabled: bool | None = None


class SessionSetMemoryResult(BaseModel):
    generate_enabled: bool
    use_enabled: bool


class SessionSetStatsCommand(BaseModel):
    type: Literal["session.set_stats"] = "session.set_stats"
    session_id: str
    stats: dict[str, Any]


class SessionSetStatsResult(BaseModel):
    stats: dict[str, Any]


class MemorySearchCommand(BaseModel):
    type: Literal["memory.search"] = "memory.search"
    query: str = ""
    session_id: str | None = None
    workspace_root: str | None = None
    limit: int = 5


class MemorySearchResult(BaseModel):
    memories: list[MemoryRecord]


class MemoryListCommand(BaseModel):
    type: Literal["memory.list"] = "memory.list"
    session_id: str | None = None
    workspace_root: str | None = None
    scope: MemoryScope | None = None
    include_deleted: bool = False
    limit: int = 100


class MemoryListResult(BaseModel):
    memories: list[MemoryRecord]
    raw_items: list[RawMemoryItem] = Field(default_factory=list)


class MemoryDeleteCommand(BaseModel):
    type: Literal["memory.delete"] = "memory.delete"
    memory_id: str
    reason: str = "user_requested"


class MemoryDeleteResult(BaseModel):
    deleted: bool


# 根据 type 字段决定命令类型的判别联合
Command = Annotated[
    PingCommand
    | CoreShutdownCommand
    | AgentRunCommand
    | EventSubscribeCommand
    | SessionCreateCommand
    | SessionListCommand
    | SessionResumeCommand
    | SessionSetWorkspaceCommand
    | SessionSendMessageCommand
    | SessionGetHistoryCommand
    | SessionCloseCommand
    | PermissionRespondCommand
    | SessionCompactCommand
    | SessionSetMemoryCommand
    | SessionSetStatsCommand
    | MemorySearchCommand
    | MemoryListCommand
    | MemoryDeleteCommand,
    Discriminator("type"),
]
