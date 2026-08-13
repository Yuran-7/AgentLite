# 运维手册（RUNBOOK）

## 日常操作

### 启动守护进程

```bash
uv run kama core start
```

默认在后台监听 `127.0.0.1:7437`，支持 Windows、Linux 和 macOS。
调试时也可以运行 `uv run kama-core` 保持在前台，按 `Ctrl+C` 优雅退出。

### 验证连通

```bash
uv run kama ping
# → pong server=0.0.1 uptime=12ms latency=2ms
```

### 停止守护进程

```bash
uv run kama core stop
```

停止命令通过本机 TCP IPC 请求 daemon 清理任务、MCP、trace 和 socket，不依赖 Unix 信号。

### Shell 工具

对外协议为兼容旧版本仍使用工具名 `bash`，实际执行器按平台选择：

- Windows：优先 `pwsh`，否则使用系统自带的 Windows PowerShell，最后回退到 `cmd.exe`。
- Linux / macOS：使用 `/bin/sh`。

命令语法需要与当前平台一致。Windows 权限检查会识别盘符绝对路径、UNC 路径、
`%USERPROFILE%`、`$env:USERPROFILE` 和 `Set-Location` 等越界形式。

---

## 配置

优先级（低 → 高）：**内建默认值 → `~/.kama/config.toml` → `.kama/config.toml` → `.env` → 系统环境变量**。

### `~/.kama/config.toml`

```toml
[core]
host = "127.0.0.1"
port = 7437

[logging]
level  = "INFO"
file   = "~/.kama/logs/core.log"
format = "text"    # "text" | "json"

[session]
dir = ".kama/sessions"  # 相对路径以 daemon 启动目录为基准

[agent]
max_steps = 20
# 所有子 Agent 的能力上限；默认不允许联网。角色 allowed_tools 只能继续收窄。
subagent_allowed_tools = [
  "read_file", "bash", "write_file", "list_dir",
  "task_create", "task_update", "task_list", "task_get",
  "spawn_agent", "agent_result",
]

[web]
enabled = true
search_provider = "duckduckgo"  # duckduckgo | brave | searxng
# search_base_url = "https://search.example.com"  # SearXNG 时必填
search_max_results = 10
timeout_s = 15
fetch_max_chars = 12000
fetch_max_bytes = 2000000
fetch_max_redirects = 5
browser_enabled = true
browser_headless = true
browser_timeout_s = 20
browser_idle_timeout_s = 600
browser_max_nodes = 100
browser_max_chars = 8000
browser_extract_limit = 10

[llm]
protocol = "anthropic"       # "anthropic" | "openai"
default_model = "deepseek-chat"
# base_url = "https://example.com"  # 可选；留空时读取对应 SDK 的标准环境变量
```

### `.env`

从 `.env.example` 复制后修改，存放本机配置与密钥（不提交 git）：

```bash
cp .env.example .env
```

### 系统环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KAMA_CONFIG` | `~/.kama/config.toml` | 覆盖配置文件路径 |
| `KAMA_HOST` | `127.0.0.1` | TCP 监听地址 |
| `KAMA_PORT` | `7437` | TCP 监听端口 |
| `KAMA_SESSIONS_DIR` | `.kama/sessions` | Session 存储目录；相对路径基于 daemon 启动目录 |
| `KAMA_LOG_LEVEL` | `INFO` | 日志级别（DEBUG / INFO / WARNING / ERROR） |
| `KAMA_LOG_FILE` | `~/.kama/logs/core.log` | 日志文件路径（留空则仅输出 stderr） |
| `KAMA_LOG_FORMAT` | `text` | 日志格式（`text` 或 `json`） |
| `LLM_PROTOCOL` | `anthropic` | DeepSeek API 协议（`anthropic` 或 `openai`） |
| `LLM_DEFAULT_MODEL` | `deepseek-chat` | 服务端实际提供的 DeepSeek model ID |
| `LLM_BASE_URL` | 空 | 通用 API 地址覆盖；否则读取 `ANTHROPIC_BASE_URL` 或 `OPENAI_BASE_URL` |
| `LLM_API_KEY` | 空 | 通用密钥覆盖；否则读取 `ANTHROPIC_API_KEY` 或 `OPENAI_API_KEY` |
| `KAMA_WEB_ENABLED` | `true` | 是否为根 Agent 注册联网工具 |
| `KAMA_WEB_SEARCH_PROVIDER` | `duckduckgo` | 搜索后端：`duckduckgo`、`brave` 或 `searxng` |
| `KAMA_WEB_SEARCH_BASE_URL` | 空 | SearXNG 实例地址 |
| `KAMA_WEB_SEARCH_API_KEY` | 空 | Brave Search API Key；不会出现在配置日志中 |
| `KAMA_BROWSER_ENABLED` | `true` | 是否注册受限 Playwright `browser` 工具 |
| `KAMA_BROWSER_HEADLESS` | `true` | Chromium 是否使用无界面模式 |
| `KAMA_BROWSER_IDLE_TIMEOUT_S` | `600` | 等待用户登录的浏览器 Session 空闲关闭秒数 |
| `KAMA_SUBAGENT_ALLOWED_TOOLS` | 见上方 `[agent]` | 逗号分隔的子 Agent 全局工具能力上限 |

OpenAI-compatible 协议使用 Chat Completions；Anthropic-compatible 协议使用 Messages API。
可以同时在 `.env` 保存两套标准 URL 和密钥，切换时只修改 `LLM_PROTOCOL`。

### 联网工具边界

- `web_search` 只返回少量结构化搜索结果；默认使用免 Key 的 DuckDuckGo。
- `web_fetch` 只读取公共 HTTP(S) 静态内容，不执行 JavaScript；会阻止 localhost、
  私有/链路本地地址和带凭据 URL，并在每次重定向后重新检查目标。
- `browser` 支持 `open/snapshot/click/type/extract/request_user_login/check_login/close`；
  禁止下载、任意 JavaScript和私网访问，单次 snapshot/extract 有硬输出上限。
- 遇到强制登录或人机验证时，根 Agent 可调用 `request_user_login`。工具会在需要时把无头
  Chromium 重启为可见窗口并保留当前 Session；用户完成操作并回复“已登录”后，Agent 通过
  `check_login` 复用页面继续。显式关闭 Session、关闭 core 或等待超时都会清理浏览器进程。
- `click` 和 `type` 默认进入权限审批，可由用户的 always 决策缓存；其他读取动作默认允许。
- 根 Agent 在 `[web].enabled = true` 时获得搜索、抓取和浏览器工具。
- 子 Agent 默认不获得联网工具。需要时先把工具加入
  `agent.subagent_allowed_tools`，再加入对应 `.kama/agents/<role>.toml` 的
  `allowed_tools`；两层取交集。匿名子 Agent 只受全局能力上限约束。即使子 Agent 获得
  `browser`，也不能发起用户登录接管。

首次安装或 Playwright 升级后，需要安装 Chromium：

```bash
uv run playwright install chromium
```

---

## 开发

```bash
uv run ruff check src tests scripts   # lint
uv run mypy src                       # 类型检查
uv run pytest tests/ -v               # 全量测试
uv run pytest tests/unit/ -v         # 仅单元测试（无需启动 daemon）

make docs                             # 重新生成 WIRE_PROTOCOL.md
make verify-s0                        # 完整验证（lint + 类型 + 测试 + 协议同源检查）
```

---

## 日志

```bash
tail -f ~/.kama/logs/core.log
```

---

## 常见错误

| 报错 | 原因 | 处理 |
|------|------|------|
| `core already running at 127.0.0.1:7437` | 已有守护进程在运行 | `uv run kama core stop` |
| `core not running` | 未启动守护进程 | `uv run kama-core` |
| `Address already in use` | 端口被其他进程占用 | `KAMA_PORT=8000 uv run kama-core` |
| `Config error: KAMA_PORT must be an integer` | `.env` 或环境变量中端口值非整数 | 检查 `KAMA_PORT` 的值 |
