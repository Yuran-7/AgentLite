from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SessionStatus = Literal["active", "waiting_for_input", "closed"]
SessionMode = Literal["one_shot", "chat"]


@dataclass
class Session:
    id: str
    mode: SessionMode
    status: SessionStatus
    title: str
    created_at: str
    updated_at: str
    workspace_root: str | None = None
    run_ids: list[str] = field(default_factory=list)

    # 将 Session 转为可写入 meta.json 的普通 dict
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "workspace_root": self.workspace_root,
            "run_ids": list(self.run_ids),
        }

    # 从 meta.json 的 dict 还原 Session 对象
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            id=str(data["id"]),
            mode=data["mode"],
            status=data["status"],
            title=str(data.get("title", "")),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            workspace_root=(
                str(data["workspace_root"])
                if data.get("workspace_root") is not None
                else None
            ),
            run_ids=[str(x) for x in data.get("run_ids", [])],
        )
