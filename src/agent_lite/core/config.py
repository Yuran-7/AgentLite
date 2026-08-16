from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7437
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE = "~/.kama/logs/core.log"
_DEFAULT_LOG_FORMAT = "text"
_DEFAULT_CONFIG_PATH = "~/.kama/config.toml"
_DEFAULT_MAX_STEPS = 20
_DEFAULT_LLM_PROTOCOL = "anthropic"
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_TRACE_FILE = "~/.kama/traces/daemon.jsonl"
_DEFAULT_SESSIONS_DIR = "~/.kama/sessions"
_DEFAULT_SUBAGENT_ALLOWED_TOOLS = [
    "read_file",
    "shell",
    "write_file",
    "list_dir",
    "task_create",
    "task_update",
    "task_list",
    "task_get",
    "spawn_agent",
    "agent_result",
]


# 将旧版 bash 工具名迁移为 shell，并保持配置顺序去重
def _normalize_tool_names(names: list[str]) -> list[str]:
    return list(dict.fromkeys("shell" if name == "bash" else name for name in names))


@dataclass
class LoggingConfig:
    level: str = _DEFAULT_LOG_LEVEL
    file: str = _DEFAULT_LOG_FILE
    format: str = _DEFAULT_LOG_FORMAT  # "text" | "json"


@dataclass
class AgentConfig:
    max_steps: int = _DEFAULT_MAX_STEPS
    # Global capability ceiling for every child agent. Role allowlists can only narrow it.
    subagent_allowed_tools: list[str] = field(
        default_factory=lambda: list(_DEFAULT_SUBAGENT_ALLOWED_TOOLS)
    )


@dataclass
class WebConfig:
    enabled: bool = True
    search_provider: str = "duckduckgo"  # "duckduckgo" | "brave" | "searxng"
    search_base_url: str = ""             # required only for searxng
    search_api_key: str = field(default="", repr=False)
    search_max_results: int = 10
    timeout_s: float = 15.0
    fetch_max_chars: int = 12_000
    fetch_max_bytes: int = 2_000_000
    fetch_max_redirects: int = 5
    browser_enabled: bool = True
    browser_headless: bool = True
    browser_timeout_s: float = 20.0
    browser_idle_timeout_s: float = 600.0
    browser_max_nodes: int = 100
    browser_max_chars: int = 8_000
    browser_extract_limit: int = 10
    user_agent: str = "KamaClaude/0.0.1 (+https://github.com/)"


@dataclass
class LlmConfig:
    protocol: str = _DEFAULT_LLM_PROTOCOL  # "anthropic" | "openai"
    default_model: str = _DEFAULT_MODEL
    base_url: str = ""  # 留空时由对应 SDK 的标准环境变量决定
    router: str = "static"  # "static" | "rule_based" (S4) | "cost_budget" (S6)


@dataclass
class TraceConfig:
    enabled: bool = True
    file: str = _DEFAULT_TRACE_FILE
    include_llm_payload: bool = True  # false 时 LLM 记录只保留摘要


@dataclass
class PermissionConfig:
    timeout_s: float = 60.0  # 审批超时秒数；0 表示不超时


@dataclass
class CompactionConfig:
    # context_pct 触发自动压缩的阈值（0 表示禁用，推荐用手动 /compact）
    auto_threshold: float = 0.0
    tool_result_limit: int = 8_000  # tool_result 截断触发字符数
    tool_result_keep: int = 4_000   # 截断后保留的前缀字符数


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"       # "stdio" | "tcp"
    command: str = ""              # stdio 专用：可执行文件路径
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    host: str = "localhost"        # tcp 专用
    port: int = 3000               # tcp 专用


@dataclass
class McpConfig:
    servers: list[McpServerConfig] = field(default_factory=list)


# Session 存储配置：默认使用全局 ~/.kama/sessions，允许通过配置或环境变量显式覆盖
@dataclass
class SessionConfig:
    dir: str = _DEFAULT_SESSIONS_DIR


@dataclass
class KamaConfig:
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    web: WebConfig = field(default_factory=WebConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    session: SessionConfig = field(default_factory=SessionConfig)


# 构建并返回运行时配置：默认值 → 全局 TOML → 项目本地 TOML → .env → 系统环境变量（后者优先级最高）
def get_config() -> KamaConfig:
    config = KamaConfig() # 所谓的内建默认值

    # .env 必须在读取 KAMA_CONFIG 之前加载，以便 .env 中的 KAMA_CONFIG 能影响 TOML 路径
    load_dotenv(".env", override=False)

    # 若显式指定 KAMA_CONFIG，只读该文件；否则按优先级叠加：全局 → 项目本地
    explicit = os.environ.get("KAMA_CONFIG")
    if explicit:
        config_paths = [Path(explicit).expanduser()]
    else:
        config_paths = [
            Path(_DEFAULT_CONFIG_PATH).expanduser(),
            Path(".kama/config.toml"),
        ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise SystemExit(f"Config parse error ({config_path}): {e}") from e
            _apply_toml(config, data)

    _apply_env(config)
    return config


# 将已解析的 TOML 根表写入 config；未知小节或类型错误时退出进程
def _apply_toml(config: KamaConfig, data: dict[str, Any]) -> None:
    known_sections = {
        "core", "logging", "agent", "web", "llm", "trace", "permission",
        "compaction", "mcp", "session",
    }
    unknown = set(data.keys()) - known_sections
    if unknown:
        raise SystemExit(f"Unknown top-level config keys: {', '.join(sorted(unknown))}")

    if "core" in data:
        core = data["core"]
        if not isinstance(core, dict):
            raise SystemExit("Config error: [core] must be a table")
        unknown_core: set[str] = set(core.keys()) - {"host", "port"}
        if unknown_core:
            raise SystemExit(f"Unknown [core] keys: {', '.join(sorted(unknown_core))}")
        if "host" in core:
            val = core["host"]
            if not isinstance(val, str):
                raise SystemExit("Config error: core.host must be a string")
            config.host = val
        if "port" in core:
            val = core["port"]
            if not isinstance(val, int):
                raise SystemExit("Config error: core.port must be an integer")
            config.port = val

    if "logging" in data:
        log = data["logging"]
        if not isinstance(log, dict):
            raise SystemExit("Config error: [logging] must be a table")
        unknown_log: set[str] = set(log.keys()) - {"level", "file", "format"}
        if unknown_log:
            raise SystemExit(f"Unknown [logging] keys: {', '.join(sorted(unknown_log))}")
        for key in ("level", "file", "format"):
            if key in log:
                val = log[key]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: logging.{key} must be a string")
                setattr(config.logging, key, val)

    if "agent" in data:
        agent = data["agent"]
        if not isinstance(agent, dict):
            raise SystemExit("Config error: [agent] must be a table")
        unknown_agent: set[str] = set(agent.keys()) - {
            "max_steps", "subagent_allowed_tools",
        }
        if unknown_agent:
            raise SystemExit(f"Unknown [agent] keys: {', '.join(sorted(unknown_agent))}")
        if "max_steps" in agent:
            val = agent["max_steps"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: agent.max_steps must be a positive integer")
            config.agent.max_steps = val
        if "subagent_allowed_tools" in agent:
            val = agent["subagent_allowed_tools"]
            if not isinstance(val, list) or not all(isinstance(item, str) for item in val):
                raise SystemExit(
                    "Config error: agent.subagent_allowed_tools must be an array of strings"
                )
            config.agent.subagent_allowed_tools = _normalize_tool_names(val)

    if "web" in data:
        web = data["web"]
        if not isinstance(web, dict):
            raise SystemExit("Config error: [web] must be a table")
        allowed_web_keys = {
            "enabled",
            "search_provider",
            "search_base_url",
            "search_max_results",
            "timeout_s",
            "fetch_max_chars",
            "fetch_max_bytes",
            "fetch_max_redirects",
            "browser_enabled",
            "browser_headless",
            "browser_timeout_s",
            "browser_idle_timeout_s",
            "browser_max_nodes",
            "browser_max_chars",
            "browser_extract_limit",
            "user_agent",
        }
        unknown_web: set[str] = set(web.keys()) - allowed_web_keys
        if unknown_web:
            raise SystemExit(f"Unknown [web] keys: {', '.join(sorted(unknown_web))}")
        if "enabled" in web:
            val = web["enabled"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: web.enabled must be a boolean")
            config.web.enabled = val
        for key in ("browser_enabled", "browser_headless"):
            if key in web:
                val = web[key]
                if not isinstance(val, bool):
                    raise SystemExit(f"Config error: web.{key} must be a boolean")
                setattr(config.web, key, val)
        if "search_provider" in web:
            val = web["search_provider"]
            if val not in {"duckduckgo", "brave", "searxng"}:
                raise SystemExit(
                    "Config error: web.search_provider must be duckduckgo, brave, or searxng"
                )
            config.web.search_provider = val
        for key in ("search_base_url", "user_agent"):
            if key in web:
                val = web[key]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: web.{key} must be a string")
                setattr(config.web, key, val)
        for key in (
            "search_max_results",
            "fetch_max_chars",
            "fetch_max_bytes",
            "fetch_max_redirects",
            "browser_max_nodes",
            "browser_max_chars",
            "browser_extract_limit",
        ):
            if key in web:
                val = web[key]
                if not isinstance(val, int) or val <= 0:
                    raise SystemExit(f"Config error: web.{key} must be a positive integer")
                setattr(config.web, key, val)
        if "timeout_s" in web:
            val = web["timeout_s"]
            if not isinstance(val, (int, float)) or val <= 0:
                raise SystemExit("Config error: web.timeout_s must be a positive number")
            config.web.timeout_s = float(val)
        for key in ("browser_timeout_s", "browser_idle_timeout_s"):
            if key in web:
                val = web[key]
                if not isinstance(val, (int, float)) or val <= 0:
                    raise SystemExit(
                        f"Config error: web.{key} must be a positive number"
                    )
                setattr(config.web, key, float(val))

    if "llm" in data:
        llm = data["llm"]
        if not isinstance(llm, dict):
            raise SystemExit("Config error: [llm] must be a table")
        unknown_llm: set[str] = set(llm.keys()) - {
            "protocol", "default_model", "base_url", "router",
        }
        if unknown_llm:
            raise SystemExit(f"Unknown [llm] keys: {', '.join(sorted(unknown_llm))}")
        if "protocol" in llm:
            val = llm["protocol"]
            if not isinstance(val, str) or val.lower() not in ("anthropic", "openai"):
                raise SystemExit(
                    "Config error: llm.protocol must be 'anthropic' or 'openai'"
                )
            config.llm.protocol = val.lower()
        if "default_model" in llm:
            val = llm["default_model"]
            if not isinstance(val, str) or not val.strip():
                raise SystemExit("Config error: llm.default_model must be a non-empty string")
            config.llm.default_model = val
        if "base_url" in llm:
            val = llm["base_url"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.base_url must be a string")
            config.llm.base_url = val
        if "router" in llm:
            val = llm["router"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.router must be a string")
            config.llm.router = val

    if "trace" in data:
        trace = data["trace"]
        if not isinstance(trace, dict):
            raise SystemExit("Config error: [trace] must be a table")
        unknown_trace: set[str] = set(trace.keys()) - {"enabled", "file", "include_llm_payload"}
        if unknown_trace:
            raise SystemExit(f"Unknown [trace] keys: {', '.join(sorted(unknown_trace))}")
        if "enabled" in trace:
            val = trace["enabled"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.enabled must be a boolean")
            config.trace.enabled = val
        if "file" in trace:
            val = trace["file"]
            if not isinstance(val, str):
                raise SystemExit("Config error: trace.file must be a string")
            config.trace.file = val
        if "include_llm_payload" in trace:
            val = trace["include_llm_payload"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.include_llm_payload must be a boolean")
            config.trace.include_llm_payload = val

    if "permission" in data:
        perm = data["permission"]
        if not isinstance(perm, dict):
            raise SystemExit("Config error: [permission] must be a table")
        unknown_perm: set[str] = set(perm.keys()) - {"timeout_s"}
        if unknown_perm:
            raise SystemExit(f"Unknown [permission] keys: {', '.join(sorted(unknown_perm))}")
        if "timeout_s" in perm:
            val = perm["timeout_s"]
            if not isinstance(val, (int, float)) or val < 0:
                raise SystemExit("Config error: permission.timeout_s must be a non-negative number")
            config.permission.timeout_s = float(val)

    if "session" in data:
        session = data["session"]
        if not isinstance(session, dict):
            raise SystemExit("Config error: [session] must be a table")
        unknown_session: set[str] = set(session.keys()) - {"dir"}
        if unknown_session:
            raise SystemExit(
                f"Unknown [session] keys: {', '.join(sorted(unknown_session))}"
            )
        if "dir" in session:
            val = session["dir"]
            if not isinstance(val, str) or not val.strip():
                raise SystemExit("Config error: session.dir must be a non-empty string")
            config.session.dir = val

    if "compaction" in data:
        comp = data["compaction"]
        if not isinstance(comp, dict):
            raise SystemExit("Config error: [compaction] must be a table")
        unknown_comp: set[str] = set(comp.keys()) - {
            "auto_threshold", "tool_result_limit", "tool_result_keep",
        }
        if unknown_comp:
            raise SystemExit(f"Unknown [compaction] keys: {', '.join(sorted(unknown_comp))}")
        if "auto_threshold" in comp:
            val = comp["auto_threshold"]
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                raise SystemExit("Config error: compaction.auto_threshold must be between 0 and 1")
            config.compaction.auto_threshold = float(val)
        if "tool_result_limit" in comp:
            val = comp["tool_result_limit"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: compaction.tool_result_limit must be a positive integer"
                )
            config.compaction.tool_result_limit = val
        if "tool_result_keep" in comp:
            val = comp["tool_result_keep"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: compaction.tool_result_keep must be a positive integer"
                )
            config.compaction.tool_result_keep = val

    if "mcp" in data:
        mcp = data["mcp"]
        if not isinstance(mcp, dict):
            raise SystemExit("Config error: [mcp] must be a table")
        unknown_mcp: set[str] = set(mcp.keys()) - {"servers"}
        if unknown_mcp:
            raise SystemExit(f"Unknown [mcp] keys: {', '.join(sorted(unknown_mcp))}")
        servers_raw = mcp.get("servers", [])
        if not isinstance(servers_raw, list):
            raise SystemExit("Config error: mcp.servers must be an array of tables")
        for i, srv in enumerate(servers_raw):
            if not isinstance(srv, dict):
                raise SystemExit(f"Config error: mcp.servers[{i}] must be a table")
            name = srv.get("name")
            if not isinstance(name, str) or not name:
                raise SystemExit(f"Config error: mcp.servers[{i}].name must be a non-empty string")
            transport = srv.get("transport", "stdio")
            if transport not in ("stdio", "tcp"):
                raise SystemExit(
                    f"Config error: mcp.servers[{i}].transport must be 'stdio' or 'tcp'"
                )
            s = McpServerConfig(name=name, transport=transport)
            if "command" in srv:
                val = srv["command"]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: mcp.servers[{i}].command must be a string")
                s.command = val
            if "args" in srv:
                val = srv["args"]
                if not isinstance(val, list):
                    raise SystemExit(f"Config error: mcp.servers[{i}].args must be an array")
                s.args = [str(a) for a in val]
            if "env" in srv:
                val = srv["env"]
                if not isinstance(val, dict):
                    raise SystemExit(f"Config error: mcp.servers[{i}].env must be a table")
                s.env = {str(k): str(v) for k, v in val.items()}
            if "host" in srv:
                val = srv["host"]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: mcp.servers[{i}].host must be a string")
                s.host = val
            if "port" in srv:
                val = srv["port"]
                if not isinstance(val, int):
                    raise SystemExit(f"Config error: mcp.servers[{i}].port must be an integer")
                s.port = val
            config.mcp.servers.append(s)


# 用 KAMA_* 环境变量覆盖 config 中对应字段（若变量已设置）
def _apply_env(config: KamaConfig) -> None:
    host = os.environ.get("KAMA_HOST")
    if host is not None:
        config.host = host

    port_str = os.environ.get("KAMA_PORT")
    if port_str is not None:
        try:
            config.port = int(port_str)
        except ValueError:
            raise SystemExit(f"Config error: KAMA_PORT must be an integer, got: {port_str!r}")

    log_level = os.environ.get("KAMA_LOG_LEVEL")
    if log_level is not None:
        config.logging.level = log_level

    log_file = os.environ.get("KAMA_LOG_FILE")
    if log_file is not None:
        config.logging.file = log_file

    log_format = os.environ.get("KAMA_LOG_FORMAT")
    if log_format is not None:
        config.logging.format = log_format

    sessions_dir = os.environ.get("KAMA_SESSIONS_DIR")
    if sessions_dir is not None:
        if not sessions_dir.strip():
            raise SystemExit("Config error: KAMA_SESSIONS_DIR must not be empty")
        config.session.dir = sessions_dir

    max_steps_str = os.environ.get("KAMA_MAX_STEPS")
    if max_steps_str is not None:
        try:
            val = int(max_steps_str)
            if val <= 0:
                raise SystemExit(
                    "Config error: KAMA_MAX_STEPS must be a positive integer,"
                    f" got: {max_steps_str!r}"
                )
            config.agent.max_steps = val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_MAX_STEPS must be an integer, got: {max_steps_str!r}"
            )

    subagent_tools = os.environ.get("KAMA_SUBAGENT_ALLOWED_TOOLS")
    if subagent_tools is not None:
        config.agent.subagent_allowed_tools = _normalize_tool_names(
            [item.strip() for item in subagent_tools.split(",") if item.strip()]
        )

    web_enabled = os.environ.get("KAMA_WEB_ENABLED")
    if web_enabled is not None:
        config.web.enabled = web_enabled.lower() not in ("0", "false", "no")

    web_provider = os.environ.get("KAMA_WEB_SEARCH_PROVIDER")
    if web_provider is not None:
        web_provider = web_provider.lower()
        if web_provider not in {"duckduckgo", "brave", "searxng"}:
            raise SystemExit(
                "Config error: KAMA_WEB_SEARCH_PROVIDER must be duckduckgo, brave, or searxng"
            )
        config.web.search_provider = web_provider

    web_base_url = os.environ.get("KAMA_WEB_SEARCH_BASE_URL")
    if web_base_url is not None:
        config.web.search_base_url = web_base_url

    web_api_key = os.environ.get("KAMA_WEB_SEARCH_API_KEY")
    if web_api_key is not None:
        config.web.search_api_key = web_api_key

    browser_enabled = os.environ.get("KAMA_BROWSER_ENABLED")
    if browser_enabled is not None:
        config.web.browser_enabled = browser_enabled.lower() not in ("0", "false", "no")

    browser_headless = os.environ.get("KAMA_BROWSER_HEADLESS")
    if browser_headless is not None:
        config.web.browser_headless = browser_headless.lower() not in ("0", "false", "no")

    browser_idle_timeout = os.environ.get("KAMA_BROWSER_IDLE_TIMEOUT_S")
    if browser_idle_timeout is not None:
        try:
            value = float(browser_idle_timeout)
        except ValueError as exc:
            raise SystemExit(
                "Config error: KAMA_BROWSER_IDLE_TIMEOUT_S must be a positive number"
            ) from exc
        if value <= 0:
            raise SystemExit(
                "Config error: KAMA_BROWSER_IDLE_TIMEOUT_S must be a positive number"
            )
        config.web.browser_idle_timeout_s = value

    default_model = os.environ.get("LLM_DEFAULT_MODEL")
    if default_model is not None:
        if not default_model.strip():
            raise SystemExit("Config error: LLM_DEFAULT_MODEL must not be empty")
        config.llm.default_model = default_model

    protocol = os.environ.get("LLM_PROTOCOL")
    if protocol is not None:
        protocol = protocol.lower()
        if protocol not in ("anthropic", "openai"):
            raise SystemExit(
                "Config error: LLM_PROTOCOL must be 'anthropic' or 'openai',"
                f" got: {protocol!r}"
            )
        config.llm.protocol = protocol

    base_url = os.environ.get("LLM_BASE_URL")
    if base_url is not None:
        config.llm.base_url = base_url

    trace_enabled = os.environ.get("KAMA_TRACE_ENABLED")
    if trace_enabled is not None:
        config.trace.enabled = trace_enabled.lower() not in ("0", "false", "no")

    trace_file = os.environ.get("KAMA_TRACE_FILE")
    if trace_file is not None:
        config.trace.file = trace_file

    trace_payload = os.environ.get("KAMA_TRACE_INCLUDE_LLM_PAYLOAD")
    if trace_payload is not None:
        config.trace.include_llm_payload = trace_payload.lower() not in ("0", "false", "no")

    perm_timeout = os.environ.get("KAMA_PERMISSION_TIMEOUT_S")
    if perm_timeout is not None:
        try:
            perm_timeout_val = float(perm_timeout)
            if perm_timeout_val < 0:
                raise SystemExit(
                    f"Config error: KAMA_PERMISSION_TIMEOUT_S must be >= 0, got: {perm_timeout!r}"
                )
            config.permission.timeout_s = perm_timeout_val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_PERMISSION_TIMEOUT_S must be a number, got: {perm_timeout!r}"
            )

    compact_threshold = os.environ.get("KAMA_COMPACT_THRESHOLD")
    if compact_threshold is not None:
        try:
            compact_threshold_val = float(compact_threshold)
            if not (0.0 <= compact_threshold_val <= 1.0):
                raise SystemExit(
                    "Config error: KAMA_COMPACT_THRESHOLD must be between 0 and 1, "
                    f"got: {compact_threshold!r}"
                )
            config.compaction.auto_threshold = compact_threshold_val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_COMPACT_THRESHOLD must be a number, got: {compact_threshold!r}"
            )

    compact_tool_limit = os.environ.get("KAMA_COMPACT_TOOL_LIMIT")
    if compact_tool_limit is not None:
        try:
            compact_tool_limit_val = int(compact_tool_limit)
            if compact_tool_limit_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_COMPACT_TOOL_LIMIT must be a positive integer, "
                    f"got: {compact_tool_limit!r}"
                )
            config.compaction.tool_result_limit = compact_tool_limit_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_COMPACT_TOOL_LIMIT must be an integer, "
                f"got: {compact_tool_limit!r}"
            )

    compact_tool_keep = os.environ.get("KAMA_COMPACT_TOOL_KEEP")
    if compact_tool_keep is not None:
        try:
            compact_tool_keep_val = int(compact_tool_keep)
            if compact_tool_keep_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_COMPACT_TOOL_KEEP must be a positive integer, "
                    f"got: {compact_tool_keep!r}"
                )
            config.compaction.tool_result_keep = compact_tool_keep_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_COMPACT_TOOL_KEEP must be an integer, "
                f"got: {compact_tool_keep!r}"
            )
