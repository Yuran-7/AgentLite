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
_DEFAULT_LOG_FILE = "~/.agentlite/logs/core.log"
_DEFAULT_LOG_FORMAT = "text"
_DEFAULT_CONFIG_PATH = "~/.agentlite/config.toml"
_DEFAULT_MAX_STEPS = 20
_DEFAULT_LLM_PROTOCOL = "anthropic"
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_TRACE_FILE = "~/.agentlite/traces/daemon.jsonl"
_DEFAULT_SESSIONS_DIR = "~/.agentlite/sessions"
_DEFAULT_MEMORY_DIR = "~/.agentlite/memories/memory.db"
_DEFAULT_SUBAGENT_ALLOWED_TOOLS = [
    "read_file",
    "shell",
    "write_file",
    "edit_file",
    "list_dir",
    "update_plan",
    "spawn_agent",
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
    user_agent: str = "AgentLite/0.0.1 (+https://github.com/)"


@dataclass
class LlmConfig:
    api_key: str | None = field(default=None, repr=False)
    protocol: str = _DEFAULT_LLM_PROTOCOL  # "anthropic" | "openai"
    default_model: str = _DEFAULT_MODEL
    context_window: int | None = None  # 显式窗口大小；未配置时使用兼容回退
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
    tool_result_keep: int = 4_000   # 头尾预览的 UTF-8 字节预算
    tool_result_token_limit: int = 4_000  # 单条模型结果估算 token 预算
    tool_result_batch_token_limit: int = 12_000  # 同一步工具结果合计估算 token 预算


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"       # "stdio" | "http" | "tcp"
    command: str = ""              # stdio 专用：可执行文件路径
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    host: str = "localhost"        # tcp 专用
    port: int = 3000               # tcp 专用
    enabled: bool = True
    url: str = ""                  # Streamable HTTP
    bearer_token_env: str = ""     # HTTP token 的环境变量名
    startup_timeout: float = 10.0


@dataclass
class McpConfig:
    servers: list[McpServerConfig] = field(default_factory=list)


# Session 存储配置：默认使用全局 ~/.agentlite/sessions，允许通过配置或环境变量显式覆盖
@dataclass
class SessionConfig:
    dir: str = _DEFAULT_SESSIONS_DIR


@dataclass
class MemoryConfig:
    dir: str = _DEFAULT_MEMORY_DIR
    use_enabled: bool = True
    generate_enabled: bool = False
    min_rollout_idle_hours: int = 6
    max_rollout_age_days: int = 10


@dataclass
class AgentLiteConfig:
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
    memory: MemoryConfig = field(default_factory=MemoryConfig)


# 构建配置：默认值 → 全局 TOML → 项目 TOML → 用户 .env → 系统环境变量
def get_config() -> AgentLiteConfig:
    config = AgentLiteConfig() # 所谓的内建默认值

    # .env 必须在读取 AGENTLITE_CONFIG 之前加载，以便它能影响 TOML 路径
    load_dotenv(Path.home() / ".agentlite/.env", override=False)

    # 若显式指定 AGENTLITE_CONFIG，只读该文件；否则按优先级叠加：全局 → 项目本地
    explicit = os.environ.get("AGENTLITE_CONFIG")
    if explicit:
        config_paths = [Path(explicit).expanduser()]
    else:
        config_paths = [
            Path(_DEFAULT_CONFIG_PATH).expanduser(),
            Path(".agentlite/config.toml"),
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


# 校验配置小节的表类型与允许字段，保留各小节原有错误提示
def _config_section(
    data: dict[str, Any], name: str, allowed: set[str],
) -> dict[str, Any]:
    section = data[name]
    if not isinstance(section, dict):
        raise SystemExit(f"Config error: [{name}] must be a table")
    unknown = set(section) - allowed
    if unknown:
        raise SystemExit(f"Unknown [{name}] keys: {', '.join(sorted(unknown))}")
    return section


# 将已解析的 TOML 根表写入 config；未知小节或类型错误时退出进程
def _apply_toml(config: AgentLiteConfig, data: dict[str, Any]) -> None:
    known_sections = {
        "core", "logging", "agent", "web", "llm", "trace", "permission",
        "compaction", "mcp", "session", "memory",
    }
    unknown = set(data.keys()) - known_sections
    if unknown:
        raise SystemExit(f"Unknown top-level config keys: {', '.join(sorted(unknown))}")

    if "core" in data:
        core = _config_section(data, "core", {"host", "port"})
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
        log = _config_section(data, "logging", {"level", "file", "format"})
        for key in ("level", "file", "format"):
            if key in log:
                val = log[key]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: logging.{key} must be a string")
                setattr(config.logging, key, val)

    if "agent" in data:
        agent = _config_section(data, "agent", {
            "max_steps", "subagent_allowed_tools",
        })
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
        web = _config_section(data, "web", {
            "enabled",
            "search_provider",
            "search_base_url",
            "search_max_results",
            "timeout_s",
            "fetch_max_chars",
            "fetch_max_bytes",
            "fetch_max_redirects",
            "user_agent",
        })
        if "enabled" in web:
            val = web["enabled"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: web.enabled must be a boolean")
            config.web.enabled = val
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

    if "llm" in data:
        llm = _config_section(data, "llm", {
            "protocol", "default_model", "base_url", "router", "context_window",
        })
        if "protocol" in llm:
            val = llm["protocol"]
            if not isinstance(val, str) or val.lower() not in ("anthropic", "openai"):
                raise SystemExit(
                    "Config error: llm.protocol must be 'anthropic' or 'openai'"
                )
            config.llm.protocol = val.lower()
        if "context_window" in llm:
            val = llm["context_window"]
            if type(val) is not int or val <= 0:
                raise SystemExit("Config error: llm.context_window must be a positive integer")
            config.llm.context_window = val
        if "default_model" in llm:
            val = llm["default_model"]
            if not isinstance(val, str) or not val.strip():
                raise SystemExit("Config error: llm.default_model must be a non-empty string")
            config.llm.default_model = val
        for key in ("base_url", "router"):
            if key in llm:
                val = llm[key]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: llm.{key} must be a string")
                setattr(config.llm, key, val)

    if "trace" in data:
        trace = _config_section(data, "trace", {"enabled", "file", "include_llm_payload"})
        for key, expected, label in (
            ("enabled", bool, "boolean"),
            ("file", str, "string"),
            ("include_llm_payload", bool, "boolean"),
        ):
            if key in trace:
                val = trace[key]
                if not isinstance(val, expected):
                    raise SystemExit(f"Config error: trace.{key} must be a {label}")
                setattr(config.trace, key, val)

    if "permission" in data:
        perm = _config_section(data, "permission", {"timeout_s"})
        if "timeout_s" in perm:
            val = perm["timeout_s"]
            if not isinstance(val, (int, float)) or val < 0:
                raise SystemExit("Config error: permission.timeout_s must be a non-negative number")
            config.permission.timeout_s = float(val)

    if "session" in data:
        session = _config_section(data, "session", {"dir"})
        if "dir" in session:
            val = session["dir"]
            if not isinstance(val, str) or not val.strip():
                raise SystemExit("Config error: session.dir must be a non-empty string")
            config.session.dir = val

    if "memory" in data:
        memory = _config_section(data, "memory", {
            "dir",
            "use_enabled",
            "generate_enabled",
            "min_rollout_idle_hours",
            "max_rollout_age_days",
        })
        if "dir" in memory:
            val = memory["dir"]
            if not isinstance(val, str) or not val.strip():
                raise SystemExit("Config error: memory.dir must be a non-empty string")
            config.memory.dir = val
        for key in ("use_enabled", "generate_enabled"):
            if key in memory:
                val = memory[key]
                if not isinstance(val, bool):
                    raise SystemExit(f"Config error: memory.{key} must be a boolean")
                setattr(config.memory, key, val)
        for key in (
            "min_rollout_idle_hours",
            "max_rollout_age_days",
        ):
            if key in memory:
                val = memory[key]
                if not isinstance(val, int) or val <= 0:
                    raise SystemExit(f"Config error: memory.{key} must be a positive integer")
                setattr(config.memory, key, val)

    if "compaction" in data:
        comp = _config_section(data, "compaction", {
            "auto_threshold", "tool_result_limit", "tool_result_keep",
            "tool_result_token_limit", "tool_result_batch_token_limit",
        })
        if "auto_threshold" in comp:
            val = comp["auto_threshold"]
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                raise SystemExit("Config error: compaction.auto_threshold must be between 0 and 1")
            config.compaction.auto_threshold = float(val)
        for key in ("tool_result_token_limit", "tool_result_batch_token_limit"):
            if key in comp:
                val = comp[key]
                if type(val) is not int or val <= 0:
                    raise SystemExit(f"Config error: compaction.{key} must be a positive integer")
                setattr(config.compaction, key, val)
        for key in ("tool_result_limit", "tool_result_keep"):
            if key in comp:
                val = comp[key]
                if not isinstance(val, int) or val <= 0:
                    raise SystemExit(f"Config error: compaction.{key} must be a positive integer")
                setattr(config.compaction, key, val)

    if "mcp" in data:
        mcp = _config_section(data, "mcp", {"servers"})
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
            if transport not in ("stdio", "tcp", "http"):
                raise SystemExit(
                    f"Config error: mcp.servers[{i}].transport must be 'stdio', 'http' or 'tcp'"
                )
            s = McpServerConfig(name=name, transport=transport)
            for key in ("url", "bearer_token_env"):
                if key in srv:
                    if not isinstance(srv[key], str):
                        raise SystemExit(f"Config error: mcp.servers[{i}].{key} must be a string")
                    setattr(s, key, srv[key])
            if "enabled" in srv:
                if type(srv["enabled"]) is not bool:
                    raise SystemExit(f"Config error: mcp.servers[{i}].enabled must be a boolean")
                s.enabled = srv["enabled"]
            if "startup_timeout" in srv:
                val = srv["startup_timeout"]
                if type(val) not in (int, float) or not 1 <= val <= 120:
                    raise SystemExit("Config error: MCP startup_timeout must be between 1 and 120")
                s.startup_timeout = float(val)
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


# 用 AGENTLITE_* 环境变量覆盖 config 中对应字段（若变量已设置）
def _apply_env(config: AgentLiteConfig) -> None:
    host = os.environ.get("AGENTLITE_HOST")
    if host is not None:
        config.host = host

    port_str = os.environ.get("AGENTLITE_PORT")
    if port_str is not None:
        try:
            config.port = int(port_str)
        except ValueError:
            raise SystemExit(f"Config error: AGENTLITE_PORT must be an integer, got: {port_str!r}")

    for key in ("level", "file", "format"):
        value = os.environ.get(f"AGENTLITE_LOG_{key.upper()}")
        if value is not None:
            setattr(config.logging, key, value)

    sessions_dir = os.environ.get("AGENTLITE_SESSIONS_DIR")
    if sessions_dir is not None:
        if not sessions_dir.strip():
            raise SystemExit("Config error: AGENTLITE_SESSIONS_DIR must not be empty")
        config.session.dir = sessions_dir

    memory_dir = os.environ.get("AGENTLITE_MEMORY_DIR")
    if memory_dir is not None:
        if not memory_dir.strip():
            raise SystemExit("Config error: AGENTLITE_MEMORY_DIR must not be empty")
        config.memory.dir = memory_dir

    memory_use = os.environ.get("AGENTLITE_MEMORY_USE_ENABLED")
    if memory_use is not None:
        config.memory.use_enabled = memory_use.lower() not in ("0", "false", "no")

    memory_generate = os.environ.get("AGENTLITE_MEMORY_GENERATE_ENABLED")
    if memory_generate is not None:
        config.memory.generate_enabled = memory_generate.lower() not in ("0", "false", "no")

    max_steps_str = os.environ.get("AGENTLITE_MAX_STEPS")
    if max_steps_str is not None:
        try:
            val = int(max_steps_str)
            if val <= 0:
                raise SystemExit(
                    "Config error: AGENTLITE_MAX_STEPS must be a positive integer,"
                    f" got: {max_steps_str!r}"
                )
            config.agent.max_steps = val
        except ValueError:
            raise SystemExit(
                f"Config error: AGENTLITE_MAX_STEPS must be an integer, got: {max_steps_str!r}"
            )

    subagent_tools = os.environ.get("AGENTLITE_SUBAGENT_ALLOWED_TOOLS")
    if subagent_tools is not None:
        config.agent.subagent_allowed_tools = _normalize_tool_names(
            [item.strip() for item in subagent_tools.split(",") if item.strip()]
        )

    web_enabled = os.environ.get("AGENTLITE_WEB_ENABLED")
    if web_enabled is not None:
        config.web.enabled = web_enabled.lower() not in ("0", "false", "no")

    web_provider = os.environ.get("AGENTLITE_WEB_SEARCH_PROVIDER")
    if web_provider is not None:
        web_provider = web_provider.lower()
        if web_provider not in {"duckduckgo", "brave", "searxng"}:
            raise SystemExit(
                "Config error: AGENTLITE_WEB_SEARCH_PROVIDER must be duckduckgo, brave, or searxng"
            )
        config.web.search_provider = web_provider

    web_base_url = os.environ.get("AGENTLITE_WEB_SEARCH_BASE_URL")
    if web_base_url is not None:
        config.web.search_base_url = web_base_url

    web_api_key = os.environ.get("AGENTLITE_WEB_SEARCH_API_KEY")
    if web_api_key is not None:
        config.web.search_api_key = web_api_key

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

    trace_enabled = os.environ.get("AGENTLITE_TRACE_ENABLED")
    if trace_enabled is not None:
        config.trace.enabled = trace_enabled.lower() not in ("0", "false", "no")

    trace_file = os.environ.get("AGENTLITE_TRACE_FILE")
    if trace_file is not None:
        config.trace.file = trace_file

    trace_payload = os.environ.get("AGENTLITE_TRACE_INCLUDE_LLM_PAYLOAD")
    if trace_payload is not None:
        config.trace.include_llm_payload = trace_payload.lower() not in ("0", "false", "no")

    perm_timeout = os.environ.get("AGENTLITE_PERMISSION_TIMEOUT_S")
    if perm_timeout is not None:
        try:
            perm_timeout_val = float(perm_timeout)
            if perm_timeout_val < 0:
                raise SystemExit(
                    "Config error: AGENTLITE_PERMISSION_TIMEOUT_S must be >= 0, "
                    f"got: {perm_timeout!r}"
                )
            config.permission.timeout_s = perm_timeout_val
        except ValueError:
            raise SystemExit(
                "Config error: AGENTLITE_PERMISSION_TIMEOUT_S must be a number, "
                f"got: {perm_timeout!r}"
            )

    compact_threshold = os.environ.get("AGENTLITE_COMPACT_THRESHOLD")
    if compact_threshold is not None:
        try:
            compact_threshold_val = float(compact_threshold)
            if not (0.0 <= compact_threshold_val <= 1.0):
                raise SystemExit(
                    "Config error: AGENTLITE_COMPACT_THRESHOLD must be between 0 and 1, "
                    f"got: {compact_threshold!r}"
                )
            config.compaction.auto_threshold = compact_threshold_val
        except ValueError:
            raise SystemExit(
                "Config error: AGENTLITE_COMPACT_THRESHOLD must be a number, "
                f"got: {compact_threshold!r}"
            )

    for name, key in (
        ("AGENTLITE_COMPACT_TOOL_LIMIT", "tool_result_limit"),
        ("AGENTLITE_COMPACT_TOOL_KEEP", "tool_result_keep"),
    ):
        value = os.environ.get(name)
        if value is not None:
            try:
                parsed = int(value)
            except ValueError:
                raise SystemExit(f"Config error: {name} must be an integer, got: {value!r}")
            if parsed <= 0:
                raise SystemExit(f"Config error: {name} must be a positive integer, got: {value!r}")
            setattr(config.compaction, key, parsed)
