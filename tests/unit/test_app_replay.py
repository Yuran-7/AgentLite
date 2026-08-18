from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_lite.core.app import CoreApp


class _Writer:
    def __init__(self) -> None:
        self.data = bytearray()

    # 收集回放接口写出的 NDJSON 字节
    def write(self, data: bytes) -> None:
        self.data.extend(data)

    # 模拟 StreamWriter 刷新接口
    async def drain(self) -> None:
        return None


# 功能：统一 session 日志按 root run 回放时应包含其 subagent，但排除其他 run
# 设计：构造同文件双 run 与父子事件，通过假 writer 检查回放边界而不启动 socket 服务
@pytest.mark.asyncio
async def test_replay_unified_session_log_filters_run_tree(tmp_path: Path) -> None:
    session_dir = (
        tmp_path / "2026" / "08" / "15" / "sess-20260815-000000-0123456789ab"
    )
    session_dir.mkdir(parents=True)
    events = [
        {"type": "run.started", "run_id": "run-one"},
        {
            "type": "subagent.started",
            "run_id": "child-one",
            "parent_run_id": "run-one",
        },
        {"type": "step.started", "run_id": "child-one"},
        {"type": "run.finished", "run_id": "run-one"},
        {"type": "run.started", "run_id": "run-two"},
        {"type": "run.finished", "run_id": "run-two"},
    ]
    (session_dir / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    app = CoreApp()
    app._sessions_root = tmp_path
    writer = _Writer()

    count = await app._replay_events(  # type: ignore[arg-type]
        "run-one",
        writer,
        ["*"],
    )

    replayed = [
        json.loads(line)["event"]
        for line in writer.data.decode().splitlines()
    ]
    assert count == 4
    assert [event["run_id"] for event in replayed] == [
        "run-one",
        "child-one",
        "child-one",
        "run-one",
    ]
