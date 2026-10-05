from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from agent_lite.core.permissions.types import PermissionMode, validate_permission_mode

SessionStatus = Literal["active", "waiting_for_input", "closed"]
SessionMode = Literal["one_shot", "chat"]
MemoryGenerationStatus = Literal["idle", "running", "succeeded", "failed"]


@dataclass
class Session:
    id: str
    mode: SessionMode
    status: SessionStatus
    title: str
    created_at: str
    updated_at: str
    model_id: str | None = None
    reasoning_effort: str = ""
    permission_mode: PermissionMode = "manual"
    collaboration_mode: Literal["default", "plan"] = "default"
    last_chat_at: str | None = None
    workspace_root: str | None = None
    run_ids: list[str] = field(default_factory=list)
    ui_stats: dict[str, Any] = field(default_factory=dict)
    memory_generate_enabled: bool = False
    memory_use_enabled: bool = True
    last_memory_extracted_run_id: str | None = None
    memory_generation_status: MemoryGenerationStatus = "idle"

    # 将 Session 转为可写入 meta.json 的普通 dict
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model_id": self.model_id,
            "reasoning_effort": self.reasoning_effort,
            "permission_mode": self.permission_mode,
            "collaboration_mode": self.collaboration_mode,
            "mode": self.mode,
            "status": self.status,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_chat_at": self.last_chat_at,
            "workspace_root": self.workspace_root,
            "run_ids": list(self.run_ids),
            "ui_stats": dict(self.ui_stats),
            "memory_generate_enabled": self.memory_generate_enabled,
            "memory_use_enabled": self.memory_use_enabled,
            "last_memory_extracted_run_id": self.last_memory_extracted_run_id,
            "memory_generation_status": self.memory_generation_status,
        }

    # 从 meta.json 的 dict 还原 Session 对象
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            id=str(data["id"]),
            model_id=data.get("model_id"),
            reasoning_effort=data.get("reasoning_effort", ""),
            permission_mode=validate_permission_mode(data.get("permission_mode", "manual")),
            collaboration_mode=data.get("collaboration_mode", "default"),
            mode=data["mode"],
            status=data["status"],
            title=str(data.get("title", "")),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            last_chat_at=(
                str(data["last_chat_at"])
                if data.get("last_chat_at") is not None
                else None
            ),
            workspace_root=(
                str(data["workspace_root"])
                if data.get("workspace_root") is not None
                else None
            ),
            run_ids=[str(x) for x in data.get("run_ids", [])],
            ui_stats=(dict(data["ui_stats"]) if isinstance(data.get("ui_stats"), dict) else {}),
            memory_generate_enabled=bool(data.get("memory_generate_enabled", False)),
            memory_use_enabled=bool(data.get("memory_use_enabled", True)),
            last_memory_extracted_run_id=(
                str(data["last_memory_extracted_run_id"])
                if data.get("last_memory_extracted_run_id") is not None
                else None
            ),
            memory_generation_status=data.get("memory_generation_status", "idle"),
        )
