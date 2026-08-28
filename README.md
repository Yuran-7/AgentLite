# 环境初始化

## 安装到项目根目录（.venv）
项目开始时只有 `.python-version` 和 `pyproject.toml`：前者指定 Python 版本，后者声明项目信息与依赖。执行：

```bash
uv sync
```

uv 会根据 `.python-version` 选择 Python：本机存在兼容版本时直接使用；不存在时，默认自动下载对应版本，再用它创建 `.venv/`。虚拟环境中的解释器位于 Windows 的 `.venv/Scripts/python.exe` 或 macOS/Linux 的 `.venv/bin/python`。

随后 uv 会解析并锁定 `pyproject.toml` 中的依赖，将依赖同步到 `.venv/`，并生成 `uv.lock`。因此项目会从最初的 `.python-version` 和 `pyproject.toml`，增加 `.venv/` 与 `uv.lock`；前者存放隔离环境，后者记录确定的依赖版本。

## 安装到本地 Python 环境

如果不使用项目虚拟环境，而要安装到本机 Python 3.12，先退出已激活的虚拟环境，再执行：

使用 uv：

```bash
deactivate
uv pip install --system --python 3.12 -e .
```

`--system` 会忽略项目中的 `.venv/`，`--python 3.12` 指定本地解释器，`-e` 表示源码修改后立即生效。安装完成后可执行 `lite`、`lite-core` 或 `lite-tui`；卸载使用：

```bash
uv pip uninstall --system --python 3.12 AgentLite
```

不使用 uv 时，先用 `python --version` 确认当前是 Python 3.12，再使用 pip：

```bash
deactivate
python --version
python -m pip install -e .
```

对应的卸载命令为：

```bash
python -m pip uninstall AgentLite
```

# Permission

## 权限检查流程

AgentLite 在工具真正执行前统一检查权限。工具参数先经过 schema 校验，校验通过后由 `PermissionManager` 按以下顺序处理：

1. shell/bash 命中 `deny_patterns` 时自动拒绝。
2. shell/bash 访问当前工作目录之外的路径，或执行目录切换、使用 home 环境变量等高风险操作时，强制要求确认；该规则不能被 `allow_patterns` 或缓存绕过。
3. 检查当前 session 的 `always` 决策，再检查跨 session 的持久化 `always` 决策。
4. shell/bash 命中 `allow_patterns` 时自动允许。
5. 使用工具默认策略；未登记的工具默认要求确认。

默认策略为：`read_file`、`list_dir`、`web_search`、`web_fetch` 自动允许；`shell`、`bash` 和 `write_file` 要求确认；未知工具也要求确认。浏览器的 `click` 和 `type` 属于交互操作，默认要求确认。

需要确认时，Core 会发布 `permission.requested` 事件并暂停当前工具调用，直到客户端返回以下决策之一：`allow_once`、`always_allow`、`deny_once`、`always_deny`。一次性决策只影响当前调用；`always` 决策会写入当前 session 的内存缓存和 `~/.agentlite/policy.toml`，后续 session 也会生效。

审批默认超时 60 秒，可通过配置或环境变量 `AGENTLITE_PERMISSION_TIMEOUT_S` 修改；设置为 `0` 表示不超时。超时、客户端断连或明确拒绝都会阻止工具执行，并返回 `permission_denied`；客户端断连时，当前 session 中所有等待审批的请求都会自动拒绝。

## 权限与沙箱的区别

权限系统回答的是“这次工具调用是否允许执行”，沙箱回答的是“即使命令已经执行，它最多能访问和影响什么”。二者不是同一层：

```text
PermissionManager  -> 执行前审批
操作系统沙箱       -> 执行中的强制隔离
```

`workspace_root`、`cwd` 和路径启发式规则只能提供工作目录约束或风险提示，不能阻止进程通过绝对路径、环境变量、子进程或其他系统接口访问资源。当前 AgentLite 的 `shell` 工具通过 Python `subprocess` 启动普通用户进程，因此项目目前没有实现操作系统级沙箱。

## 什么是沙箱

沙箱是操作系统或容器提供的受限制执行环境。工具进程在其中运行时，会受到文件系统、网络、子进程和资源权限的约束；它创建的子进程通常继承这些限制。沙箱不一定是一个独立线程或单独的“沙箱进程”，更准确地说，是施加在进程及其进程树上的隔离策略。

可靠的沙箱通常需要操作系统强制执行以下一种或多种限制：

- 只允许访问挂载的 workspace，禁止读取其他目录；
- 禁止或限制网络连接；
- 使用低权限用户或受限令牌；
- 限制 CPU、内存、运行时间和子进程数量；
- 防止通过子进程、软链接或环境变量绕过限制。

## Python 如何操作操作系统实现沙箱

Python 负责启动和管理受限进程，真正执行隔离的是操作系统或容器运行时。常见结构是：

```text
ShellTool -> SandboxRunner -> 受限进程 -> shell 命令
```

在 Windows 上，Python 可以通过 `ctypes` 或第三方库调用 Windows API，例如使用受限令牌启动进程、用 Job Object 限制资源、用 NTFS ACL 限制目录访问，并配合防火墙限制网络。Linux 上可以调用或启动 namespaces、`seccomp`、Landlock、容器等机制。

### 不依赖 Docker 的实现

非 Docker 方案的核心仍然是“Python 调用操作系统能力”，只是由程序自己负责创建和清理隔离环境。

在 Windows 上，`SandboxRunner` 通常需要组合以下步骤：

1. 创建专用的低权限用户，或者通过 `CreateRestrictedToken` 创建受限令牌；
2. 给该用户只授予 workspace 的读写权限，使用 NTFS ACL 拒绝其他目录；
3. 使用 `CreateProcessAsUser`，让 PowerShell/cmd 在受限令牌下启动；
4. 将进程加入 Job Object，限制运行时间、内存、CPU 和子进程数量；
5. 根据需要配置 Windows Firewall，禁止该进程访问网络。

Python 可以通过 `ctypes` 直接调用这些 Windows API，也可以使用 `pywin32` 等库封装 API。此时 `subprocess` 仍然负责启动命令，但不能只设置 `cwd`：

```python
# 伪代码：关键 API 的调用顺序，不是完整的 Windows 安全实现
token = win32security.CreateRestrictedToken(...)
job = win32job.CreateJobObject(None, "AgentLiteSandbox")
win32job.SetInformationJobObject(job, limits)
process = win32process.CreateProcessAsUser(
    token,
    powershell_path,
    command_line,
    ...,  # 受限令牌、workspace 环境和启动参数
)
win32job.AssignProcessToJobObject(job, process_handle)
```

在 Linux 上，可以由 Python 启动一个隔离的命令进程，并组合 namespaces、Landlock、seccomp、`setrlimit` 和低权限用户。例如，下面的命令使用 `bwrap` 创建临时文件系统视图，只暴露 workspace，并关闭网络：

```python
await asyncio.create_subprocess_exec(
    "bwrap",
    "--die-with-parent",
    "--unshare-net",
    "--ro-bind", "/usr", "/usr",
    "--ro-bind", "/bin", "/bin",
    "--proc", "/proc",
    "--dev", "/dev",
    "--bind", str(workspace), "/workspace",
    "--chdir", "/workspace",
    "/bin/sh", "-c", command,
)
```

如果只需要限制资源，而不需要完整文件和网络隔离，也可以在 Python 子进程启动前设置 `resource.setrlimit`。但这只能限制 CPU、内存、文件大小等资源，不能单独构成沙箱；Windows 也没有完全等价的 `resource` 模块，通常需要使用 Job Object。

也可以让 Python 启动 Docker 等现成沙箱运行时：

```python
await asyncio.create_subprocess_exec(
    "docker", "run", "--rm",
    "--network", "none",
    "--read-only",
    "-v", f"{workspace}:/workspace",
    "agentlite-sandbox",
    "sh", "-lc", command,
)
```

这里 Python 只是控制沙箱的生命周期和参数，文件、网络及进程隔离由 Docker 和操作系统负责。相比在 `bash.py` 中继续增加正则规则，这种方式安全边界更明确，但需要处理镜像、权限、跨平台差异、超时、资源限制和异常清理。

# Multi-Agent

## 子 Agent 的启动方式

多智能体能力通过 `spawn_agent` 工具触发。父 Agent 需要提供子任务描述和完整 prompt；子 Agent 不会自动读取父 Agent 的对话历史，因此 prompt 必须包含它所需的上下文。可选的 `subagent_type` 会加载对应角色配置，覆盖子 Agent 的 system prompt。

```text
父 Agent
  └─ spawn_agent(description, prompt, subagent_type)
       └─ 创建子 run、执行上下文、事件总线和工具注册表
```

## Session 与 Run 的关系

子 Agent 不会创建独立 session，而是在父 session 中创建新的 `run_id`。每个子 Agent 拥有独立的 `ExecutionContext` 和 `AgentLoop`，但复用父 Agent 的：

- `session_id`；
- workspace；
- 权限管理器；
- LLM provider；
- 后台任务注册表。

因此数据关系是：

```text
一个 session
├─ 父 Agent 的 run_id
├─ 子 Agent 的 run_id
└─ 其他子 Agent 的 run_id
```

如果父 Agent 属于持久化 session，子 Agent 的事件会随父 session 写入同一个 `events.jsonl`；子 Agent 不会单独生成 session 文件夹，也不会把自己的对话历史追加为独立 session 消息。

## 上下文和工具隔离

子 Agent 是“冷启动”执行：它使用新建的 `ExecutionContext`，初始目标是 `spawn_agent` 的 `prompt`，不继承父 Agent 的消息列表。子 Agent 会新建自己的 `ToolRegistry`，再根据全局配置和角色 profile 的 `allowed_tools` 过滤工具。

默认情况下，子 Agent 可以使用文件、shell、计划、再次派生子 Agent 和查询后台结果等工具；实际可用工具不能超过 `agent.subagent_allowed_tools` 设置的全局上限。角色 profile 只能进一步缩小权限，不能扩大这个上限。

虽然子 Agent 有独立的执行上下文，但它复用父 session 的 `PermissionManager` 和 `session_id`。因此子 Agent 的工具调用仍然要经过权限检查，`always` 决策也属于同一个 session 的权限上下文。当前实现没有为子 Agent 提供独立的操作系统沙箱。

## 前台与后台执行

`run_in_background=false` 时，父 Agent 会等待子 Agent 完成，然后直接获得子 Agent 的最终文本结果。`run_in_background=true` 时，工具立即返回 `run_id`，子 Agent 由 `asyncio.create_task` 在后台运行；父 Agent 后续通过 `agent_result(run_id)` 查询结果或等待其完成。

后台子 Agent 由共享的 `BackgroundTaskRegistry` 管理，支持状态查询、超时等待和异常返回。前台和后台模式使用相同的子 Agent 创建流程，区别只在于父 Agent 是否等待执行结束。

## 事件转发与 TUI 展示

每个子 Agent 都有自己的 `EventBus`，避免直接共享执行循环；子 bus 的事件会通过 bridge 转发到父 bus。启动和结束时分别发布 `subagent.started`、`subagent.finished` 事件，TUI 根据 `run_id` 和 `parent_run_id` 展示嵌套进度。

```text
子 Agent EventBus
        ↓ bridge
父 Agent EventBus
        ├─ session EventAppender：写入 session/events.jsonl
        └─ 全局事件订阅者：推送到 TUI/CLI
```

## 子 Agent 嵌套限制

子 Agent 支持有限的嵌套派生，最大嵌套深度为 2。这样可以表达“父 Agent → 子 Agent → 孙 Agent”的协作关系，同时避免无限递归创建任务。工具注册表只会在允许的深度和工具白名单范围内注册 `spawn_agent`。
