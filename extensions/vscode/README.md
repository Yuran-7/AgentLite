# AgentLite VS Code 插件

这是现有 `lite-core` 的图形客户端。聊天界面默认位于右侧辅助侧边栏，与 Codex、Chat 并列，TypeScript 扩展宿主使用 TCP / JSON-RPC / NDJSON 连接 Python core。
支持多轮消息、流式 Markdown、工具参数与结果、计划、权限确认、取消，以及模型和 token 状态。不会启动或解析 TUI。

## 启动修复（0.0.44）

本地端口暂时被拒绝绑定或占用时，core 在原端口最多重试 4 秒；仍失败时显示具体端口原因和设置入口说明。初始化失败也会关闭已启动的 MCP 进程与 trace。Windows npm 安装的工具入口（包括 scoped package 的 `.cmd`）直接使用 Node 启动，并保留启动参数。

本版同时修改 Python core 源码。源码安装使用当前仓库即可；其他机器需要同步更新 AgentLite Python 包，单独安装 VSIX 不会替换 Python 后端。

## 收藏回答（0.0.14）

顶部书签图标打开当前会话的收藏面板，点击模型回答下方的书签图标可收藏或取消收藏。已收藏的图标实心显示；生成过程中禁止新增收藏，避免保存未完成的内容。

面板列出回答摘要和时间，选中后展示完整 Markdown，并提供“在聊天中查看”和取消收藏操作。原文不在当前展示中时仍可阅读保存的快照。宽屏并排展示，窄侧栏覆盖展示，可通过右上角关闭按钮或 Escape 返回。

收藏保存在 VS Code 当前工作区的本地扩展状态，按会话隔离；重载窗口、恢复会话后仍然保留，不发送到模型。不跨不同工作区同步。

## 聊天界面细节（0.0.13）

- 用户消息靠右，悬停或键盘聚焦后显示发送时间、复制和编辑按钮。编辑将原问题放回输入框，再次发送会追加新消息，不改写已有记录；运行中禁止编辑。
- 主任务超过 10 秒时，最终回答上方显示真实的 `Worked for 4m 21s` 耗时与分割线，点击可展开工具和计划过程。运行中显示 `Working` 计时，取消时显示 `Stopping…`。
- 输入框默认一行，随文字和侧栏宽度自动增高；删除文字或发送后自动收缩，超过最大高度后内部滚动。
- 模型选择使用带名称、副标题和选中标记的弹出菜单，支持方向键、Home/End、Enter、Escape 和点击外部关闭；运行中锁定选择。
- 回答支持复制反馈和编辑器展开，上翻聊天后可一键回到最新消息；工具过程采用无外框布局。
- “＋”上下文菜单中的文件与图片、工作区引用为禁用占位。明暗主题、窄侧栏、键盘焦点和减少动画偏好均有适配。
- 输入区的信息图标打开连接详情，包含连接状态、工作区、重连和日志；顶部标题与图标更紧凑。空会话页显示 AgentLite logo，界面标签和欢迎文案不可选中，聊天正文与输入仍可选中。

发送时间和耗时从真实消息及运行状态读取，不伪造未知历史的时间；当前后端历史记录没有这些字段时，恢复的旧消息不显示时间与耗时。

## 输入框快捷命令

在消息开头输入 `/` 打开快捷命令菜单，继续输入英文命令或中文名称即可筛选。方向键选择，Enter / Tab 确认，Esc 或点击外部关闭；Shift+Enter 仍用于换行。普通消息中的路径和斜杠不会触发菜单。

已接入 `/new`、`/history`、`/model`、`/settings`、`/status`、`/logs`、`/mcp`。`/goal`、`/plan`、`/reasoning`、`/memories`、`/skills` 保留占位入口，选择后显示待实现说明，不向模型发送请求，也不更改配置。运行中沿用输入框的锁定规则。

## MCP 服务器（0.0.43）

输入 `/mcp` 打开服务器面板，查看真实的连接状态和工具数量；展开服务器可查看连接地址、错误原因及工具说明、参数定义。面板支持刷新、添加服务器、启用/禁用、重新连接、编辑和应用配置。任务运行中可查看状态，修改连接需等待主任务和后台子任务结束。

“添加服务器”提供 STDIO、Streamable HTTP 和兼容已有服务器的 TCP 三种连接方式。STDIO 的参数填写 JSON 字符串数组，避免路径含空格时被错误拆分；Windows 的标准 npm/npx 包装器自动使用 Node CLI 启动。新工具在下一轮任务中生效，调用沿用现有权限策略。

配置保存在 `~/.agentlite/mcp.json`，所有项目和连接同一 core 的客户端共享。文件不存在时沿用 TOML 中的 `[[mcp.servers]]`；第一次编辑、添加或切换状态会创建 JSON 配置，之后以该文件为准。编辑器提供字段补全和校验，保存后在面板点击“应用配置”，只重连改动或断线的服务器；无效配置不会替换当前可用连接。测试可用 `AGENTLITE_MCP_SETTINGS` 指定独立文件。

```json
{
  "servers": [
    {
      "name": "documentation",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp"],
      "enabled": true,
      "startup_timeout": 10
    },
    {
      "name": "remote",
      "transport": "http",
      "url": "https://example.com/mcp",
      "bearer_token_env": "MY_MCP_TOKEN",
      "enabled": false
    }
  ]
}
```

HTTP 支持 JSON 与 SSE 响应、MCP 会话和 Bearer Token。Token 值放在环境变量或 `~/.agentlite/.env`，界面只接收变量名；服务器环境变量与密钥不进入状态快照。当前尚未实现 OAuth 登录、旧式 HTTP+SSE 端点、资源与提示词浏览。

此功能同时更新 Python core。升级插件后，若旧 core 仍在运行，在任务结束后执行 `uv run lite core stop`，再点击聊天输入区信息面板中的“重新连接”；源码安装无需重装 Python 包，非源码安装需同步更新 AgentLite。旧 core 会在 MCP 面板显示明确的版本提示。

## 历史聊天与模型选择（0.0.8）

顶部左侧显示当前会话名称，点击即可改名；右侧依次是收藏回答、历史会话和新建会话图标。
点击时钟图标查看当前项目的聊天，支持按名称搜索、改名和恢复后继续追问。点击输入框工具栏的信息图标查看连接状态、重连和日志。
未手动命名时首次消息自动生成会话名称；记录保存在 core 配置的会话目录（默认 `~/.agentlite/sessions`），重载窗口后仍可恢复。
恢复的工具卡片显示原参数和结果，历史权限不会重新触发。运行中禁止切换会话和模型。

输入框内的底部工具栏可选择模型，直接显示模型名称。选择只影响当前会话的后续请求，并随会话保存；其他窗口不受影响。
新会话使用 `defaultModel`；未填写时自动使用模型列表的第一项。未配置自定义模型时显示已有 `.env` / TOML 对应的实际模型名称。

点击输入框左下角的齿轮（“模型配置”）打开用户目录 `~/.agentlite/settings.json`；不存在时创建空模型列表。
编辑器提供字段补全和校验，保存后自动刷新模型列表。
所有项目共用用户目录中的模型配置，不会在当前项目创建 `.agentlite` 目录，也不读取项目级模型 JSON。

```json
{
  "defaultModel": "my-model",
  "models": [
    {
      "id": "my-model",
      "name": "我的模型",
      "protocol": "openai",
      "model": "提供商的模型名称",
      "baseUrl": "https://your-provider.example/v1",
      "apiKeyEnv": "MY_MODEL_API_KEY"
    }
  ]
}
```

在用户目录 `~/.agentlite/.env` 写密钥（Windows：`C:\Users\<用户名>\.agentlite\.env`）：

```dotenv
MY_MODEL_API_KEY=填入自己的密钥
```

JSON 保存模型名称、协议和地址；`~/.agentlite/.env` 保存密钥。不要把真实密钥写进 JSON。
`protocol` 支持 `openai` / `anthropic`，`name`、`baseUrl`、`apiKeyEnv` 可省略。
省略 `apiKeyEnv` 时读取对应的 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`。
模型配置按上面的 JSON 示例填写；示例地址需要替换为你的提供商地址。

模型列表只读取用户目录 `~/.agentlite/settings.json`。
`defaultModel` 指定新会话使用的模型 id，省略时使用第一项，空字符串表示使用原有 core 配置。
密钥优先级为系统/进程环境变量 > `~/.agentlite/.env`；不读取项目或 core 启动目录下的 `.env`。
core 启动时会加载 `~/.agentlite/.env` 到进程环境，因此修改已加载的密钥后需要重启 core。
JSON 在请求时读取，修改模型名称/协议/地址无需重启。

原项目 `.env` 中的配置和密钥需迁移到 `~/.agentlite/.env`；未配置 JSON 模型时继续使用环境变量和 TOML 配置。
core 可以在尚未配置模型密钥时启动，先浏览历史或编辑配置；真正请求模型时才检查凭证。
自定义模型的 JSON 字段优先于 `LLM_PROTOCOL` / `LLM_DEFAULT_MODEL` / `LLM_BASE_URL`，密钥只读取指定变量。
模型被移除、JSON 格式错误或密钥缺失时显示错误，不自动切换到其他模型。

升级后需在没有运行任务时执行 `uv run lite core stop`，再重连插件，以加载新的后端接口。
升级插件后，执行 VS Code 的“开发人员: 重新加载窗口”，以替换内存中的旧插件。

## 从源码运行

需要本地桌面 VS Code 1.106+、Node.js 22.12+、Python 3.12，以及已经安装 AgentLite 的 Python 环境。
在 AgentLite 仓库执行：

```powershell
uv sync
cd extensions/vscode
npm install
```

回到 VS Code 的仓库窗口，在“运行和调试”中选择 **AgentLite: VS Code Demo**，按 F5。
它会先检查并构建插件，再打开扩展开发窗口。点击右侧辅助侧边栏的 AgentLite 标签，或执行命令 **AgentLite: 打开聊天**。
不需要先手动启动 `lite-core`。

## 配置与自动启动

| 设置 | 默认值 | 行为 |
| --- | --- | --- |
| `agentLite.pythonPath` | 空 | 显式路径 → 已验证的共享 Python 缓存 → 项目 `.venv` → PATH；自动候选缺少 AgentLite 时尝试下一个，显式配置错误则直接提示 |
| `agentLite.coreDirectory` | 空 | core 的启动与配置目录；自动发现 AgentLite 源码目录，普通安装使用 `~/.agentlite`。与聊天项目独立 |
| `agentLite.corePort` | `7437` | 连接 `127.0.0.1` 上的共享 core；修改后点击重试 |

解释器必须为 Python 3.12，并能导入 `agent_lite.core.app`。如果在其他代码仓库使用插件，设置为 AgentLite 仓库的虚拟环境解释器，例如：

```json
{
  "agentLite.pythonPath": "C:\\path\\to\\AgentLite\\.venv\\Scripts\\python.exe"
}
```

插件不自动安装依赖。也可以提前使用目标解释器执行 `python -m pip install -e <AgentLite仓库路径>`。

打开聊天视图后，先连接端口并进行 `core.ping` 握手。已有 core 可用时直接复用，无需检查本机解释器。
端口未监听时才执行 `python -m agent_lite.core`，并等待最多 30 秒；进程提前退出时立即报告启动失败和可识别的缺失凭证变量。host/port 由插件显式传入。
core 的工作目录由 `agentLite.coreDirectory` 指定或从安装位置发现，用于读取模型配置。选定的项目目录仅作为会话的 `workspace_root`，不会要求每个项目都安装 AgentLite 或配置 API key。多个根目录时先选择本次操作的项目。

模型和凭证仍由 core 配置。新启动的 core 按已有规则读取继承环境、用户目录中的 `~/.agentlite/.env`、全局 TOML 和 core 配置目录中的 `.agentlite/config.toml`。插件不会复制或缓存凭证，运行环境缓存只保存 Python 路径。
如果凭证只设置在某个终端中，请从该终端运行 `code .`，确保 VS Code 继承环境。
复用的 core 沿用原来的用户 `.env` / TOML 启动配置；JSON 模型配置从用户目录读取，选择只影响当前会话。

输入框下方的输入、输出和缓存数值来自最近一次主 Agent 请求；输入已包含缓存，缓存列是其中的读取量。上下文水位为 `(完整输入 + 本次输出) / 模型窗口 × 100%`，不会累计多次请求。Anthropic 的完整输入包含未缓存、缓存读取和缓存写入三个字段；OpenAI 的 `prompt_tokens` 已包含缓存，不能再相加。该数值是上次响应结束时的用量快照，新增用户消息、工具结果和压缩后的上下文要等下一次请求返回用量后更新。

可在用户 `~/.agentlite/settings.json` 的模型条目中设置正整数 `contextWindow`（token 数），或在 TOML 的 `[llm]` 中设置 `context_window`。请按实际端点容量填写；未配置时沿用回退窗口，VS Code 用 `≈` 标记估算。重启 core 并重新加载扩展后，新的用量事件会采用修正后的口径。

多个 VS Code 窗口同时打开时，通过用户目录下 `.agentlite/locks` 中按端口区分的共享启动锁协调；只有一个窗口执行预检和启动，其余窗口等待并复用。锁使用原子目录创建和定期更新时间，异常终止后过期锁可回收，参见 [锁实现说明](https://github.com/moxystudio/node-proper-lockfile)。core 已就绪时直接连接，不启动新的 Python 检查进程。

Windows 的 `.venv/Scripts/python.exe` 是转发启动器，因此一个 core 常表现为两个 `python.exe` 的父子进程：小的启动器与实际解释器。首次启动还有短暂预检和后台启动器，它们不代表多个独立 core；真正的 core 是监听配置端口的解释器进程。

## 使用与生命周期

- Enter 发送，Shift+Enter 换行；任务运行中禁止重复发送和新建会话。
- 工具卡片可折叠；超过 4000 字符的展示内容提供展开按钮。
- 权限卡片支持允许一次、始终允许、拒绝一次、始终拒绝；“始终”行为沿用 core 的权限存储语义。
- 点击停止后等待 core 的取消事件；不会关闭会话或 core，之后可继续追问。
- 隐藏或重建侧边栏时，从扩展宿主快照恢复当前展示。
- 历史会话单行显示，右侧为简短相对时间；悬停或键盘聚焦时显示归档与改名。归档保存在插件当前工作区中，不删除会话文件，可通过“已归档”入口查看和恢复。
- 连接中断时展示内容标为中断。点击重试创建新会话，不自动重新执行旧任务。
- 退出或卸载扩展时取消自己的运行、关闭自己的会话并注销前端，**不调用 `core.shutdown`**。只要还有其他 TUI 或 VS Code 前端，core 就继续运行；最后一个前端退出后等待 15 秒，再正常清理退出。关闭或隐藏聊天视图不会注销前端，重载窗口在宽限期内可以重新连接。
- 前端每 10 秒发送心跳，45 秒未续约会被视为失效；TCP 断开会立即释放租约。自动 core 启动后 60 秒没有任何前端登记，也会回收。连接图标表示当前连接状态。
- 连接信息显示当前共享 core 的 VS Code 插件和 TUI 连接数量（包含当前插件），随 10 秒心跳刷新。旧 core 未提供统计时显示“连接数量暂不可用”。
- 连接信息实时显示本次连接等待秒数，成功后固定为“连接 core 花了 X 秒”。从插件开始连接计时，包含 core 探测、启动、前端登记和会话订阅；心跳与新建聊天不会重置耗时，重新连接会重新计时。
- `uv run lite core start` 手动启动常驻 core，或者将现有自动 core 转为常驻。直接运行 `lite-core` 默认常驻；`uv run lite core stop` 可主动停止。常驻 core 不随前端退出而停止。
- 点击“日志”打开插件启动日志或默认 core 日志 `~/.agentlite/logs/core.log`；自定义日志位置以 core 配置为准。启动诊断位于扩展的 globalStorage 目录下 `core-launch.log`。

只有受信任的本地文件夹支持启动。目前不支持 Remote / WSL / 虚拟工作区、文件引用、diff 审阅或 Marketplace 发布。
新增 `frontend.register`、`frontend.heartbeat`、`frontend.unregister` 和 `core.keep_alive` 管理生命周期，登记绑定 TCP 连接；普通 CLI 查询不会计入前端。其他接口保持兼容。长任务的 `session.send_message` 响应在整轮结束后返回，运行状态由事件驱动。

## 构建、测试与安装

在 `extensions/vscode` 执行：

```powershell
npm run check
npm test
npm run package
```

`npm test` 自动构建后运行真实 TCP 客户端测试、进程启动替身测试、会话状态测试和打包 Webview 的 DOM 测试。
还会使用仓库 `.venv` 启动真实 Python core，只替换模型与全局权限存储，验证真实文件工具、会话、权限超时、取消和共享进程生命周期；没有解释器时该测试跳过。
可通过环境变量 `AGENTLITE_TEST_PYTHON` 指定测试解释器。测试不会调用外部模型或使用用户权限策略。

`npm run test:live` 是单独的真实模型验收入口，会使用仓库现有模型配置并产生 API 请求。文件操作、会话与权限存储隔离在临时目录，禁用 MCP；只自动批准临时验收文件的写入。
`npm run test:vscode` 在独立 profile 中启动真正的扩展宿主，验证页面就绪和会话订阅；先将 `VSCODE_EXECUTABLE` 设置为本地 VS Code 可执行文件路径。它不会修改你的日常 VS Code 配置。

Windows 后台启动使用 Python 启动器与 `CREATE_NO_WINDOW`，标准流写入日志；core 无可见终端，启动器退出后仍可运行。自动启动显式设置 `AGENTLITE_FRONTEND_MANAGED=1`，手动启动默认常驻。TUI 同样支持自动启动，日志在 `~/.agentlite/logs/frontend-launch.log`。

升级到 0.0.4 后，先执行 `uv run lite core stop` 停止旧 core，再重新加载 VS Code 或点击重试；旧进程不会自动加载新的生命周期代码。停止共享 core 会中断其他前端，应在任务结束后操作。

Logo 使用薄荷绿的字母 A 与暖金色闪电，分别代表 Agent 和 Lite；插件列表使用 `media/icon.png`，矢量源为 `media/logo.svg`，侧边栏使用同形状的单色图标。构建时自动从矢量源生成 256×256 PNG。

从 0.0.5 升级后，换项目会自动使用 AgentLite 自己的配置目录。源码安装通常无需额外设置；如果模型配置保存在其他位置，请在用户设置中填写 `agentLite.coreDirectory`。该设置不会改变会话的项目工作目录。

打包输出 `agentlite-vscode-0.0.46.vsix`。在 VS Code 扩展面板菜单选择“从 VSIX 安装”，或运行：

```powershell
code --install-extension ./agentlite-vscode-0.0.46.vsix
```

从旧版升级后，如果 VS Code 记住了左侧位置，右键 AgentLite 图标或视图标题，选择“移动到” → “辅助侧边栏”（Move To → Secondary Side Bar）。VS Code 会记住新位置。默认右侧贡献点从 VS Code 1.106 起正式支持，参见 [官方发布说明](https://code.visualstudio.com/updates/v1_106#_view-containers-in-secondary-side-bar)。

真实模型手工验收：打开侧边栏，让 Agent 读取工作区文件并总结，继续追问，然后请求写入一个临时文件并确认权限，最后取消一个进行中的任务，确认还能继续对话。
UI 自动测试使用 DOM 环境；实际 VS Code 布局、焦点和主题效果仍应在开发窗口中检查。

## 图片消息（0.0.45）

输入框左下角的「+ → 上传图片」可选择图片，也支持粘贴截图或拖入图片文件。发送前可预览、移除图片；可只发图片，或附带文字。编辑消息时会恢复图片附件，历史会话也保留图片。

支持 PNG、JPEG、GIF、WebP，每条消息最多 4 张，每张最多 3 MiB，单边最多 8192 像素、总像素最多 3200 万。图片随会话保存在本地，并作为原生图片消息块发送给 Anthropic 或 OpenAI-compatible 接口；选择的模型需支持视觉输入。

