from __future__ import annotations

import os
from pathlib import Path

import agent_lite.core.app as app_module
from agent_lite.core.config import get_config
from agent_lite.core.permissions.manager import PermissionManager


# 读取已有模型配置，但将手工验收的存储、策略和工具集隔离到临时目录。
def main() -> None:
    config = get_config()
    root = Path(os.environ["AGENTLITE_TEST_DIR"])
    config.port = int(os.environ["AGENTLITE_PORT"])
    config.host = "127.0.0.1"
    config.session.dir = str(root / "sessions")
    config.memory.dir = str(root / "memory.db")
    config.logging.file = str(root / "core.log")
    config.logging.level = "WARNING"
    config.trace.enabled = False
    config.web.enabled = False
    config.mcp.servers = []
    config.memory.generate_enabled = False
    config.memory.use_enabled = False
    config.agent.max_steps = 5
    app_module.get_config = lambda: config
    app_module.PermissionManager = lambda **kwargs: PermissionManager(timeout_s=60)
    app_module.run()


if __name__ == "__main__":
    main()
