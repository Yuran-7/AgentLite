from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
from pathlib import Path

from agent_lite.core.config import get_config
from agent_lite.tui.app import AgentLiteTuiApp

_DEFAULT_TUI_LOG = "~/.agentlite/logs/tui.log"


# TUI 文件日志初始化：不写 stderr（避免干扰 Textual 渲染），只写滚动文件
def _setup_logging(level: str) -> None:
    log_path = Path(os.environ.get("AGENTLITE_TUI_LOG_FILE", _DEFAULT_TUI_LOG)).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            'level=%(levelname)s ts=%(asctime)s source=%(name)s msg="%(message)s"',
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.DEBUG))
    root.handlers.clear()
    root.addHandler(handler)


# lite-tui 入口：解析 --replay 参数后启动 TUI 应用
def main() -> None:
    parser = argparse.ArgumentParser(prog="lite-tui", description="AgentLite TUI")
    parser.add_argument(
        "--replay",
        metavar="RUN_ID",
        help="Replay events from a past run on connect",
    )
    args = parser.parse_args()

    workspace = Path.cwd()
    if not workspace.exists() or not workspace.is_dir():
        parser.error(f"workspace is not a directory: {workspace}")
    workspace_root = str(workspace.resolve())

    config = get_config()
    _setup_logging(config.logging.level)
    app = AgentLiteTuiApp(
        config.host,
        config.port,
        replay_run_id=args.replay,
        llm_protocol=config.llm.protocol,
        workspace_root=workspace_root,
        model=config.llm.default_model,
    )
    app.run()


if __name__ == "__main__":
    main()
