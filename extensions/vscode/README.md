# AgentLite VS Code Demo

这是现有 `lite-core` 的图形客户端。聊天界面默认位于右侧辅助侧边栏，与 Codex、Chat 并列，TypeScript 扩展宿主使用 TCP / JSON-RPC / NDJSON 连接 Python core。
支持多轮消息、流式 Markdown、工具参数与结果、计划、权限确认、取消，以及模型和 token 状态。不会启动或解析 TUI。

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

模型和凭证仍由 core 配置。新启动的 core 按已有规则读取继承环境、core 配置目录中的 `.env` / `.agentlite/config.toml` 和全局配置。插件不会复制或缓存凭证，运行环境缓存只保存 Python 路径。
如果凭证只设置在某个终端中，请从该终端运行 `code .`，确保 VS Code 继承环境。
复用的 core 沿用它原来的启动配置，打开另一个工作区不会重新加载共享进程的模型配置。

多个 VS Code 窗口同时打开时，通过用户目录下 `.agentlite/locks` 中按端口区分的共享启动锁协调；只有一个窗口执行预检和启动，其余窗口等待并复用。锁使用原子目录创建和定期更新时间，异常终止后过期锁可回收，参见 [锁实现说明](https://github.com/moxystudio/node-proper-lockfile)。core 已就绪时直接连接，不启动新的 Python 检查进程。

Windows 的 `.venv/Scripts/python.exe` 是转发启动器，因此一个 core 常表现为两个 `python.exe` 的父子进程：小的启动器与实际解释器。首次启动还有短暂预检和后台启动器，它们不代表多个独立 core；真正的 core 是监听配置端口的解释器进程。

## 使用与生命周期

- Enter 发送，Shift+Enter 换行；任务运行中禁止重复发送和新建会话。
- 工具卡片可折叠；超过 4000 字符的展示内容提供展开按钮。
- 权限卡片支持允许一次、始终允许、拒绝一次、始终拒绝；“始终”行为沿用 core 的权限存储语义。
- 点击停止后等待 core 的取消事件；不会关闭会话或 core，之后可继续追问。
- 隐藏或重建侧边栏时，从扩展宿主快照恢复当前展示。
- 连接中断时展示内容标为中断。点击重试创建新会话，不自动重新执行旧任务。
- 退出或卸载扩展时取消自己的运行、关闭自己的会话并注销前端，**不调用 `core.shutdown`**。只要还有其他 TUI 或 VS Code 前端，core 就继续运行；最后一个前端退出后等待 15 秒，再正常清理退出。关闭或隐藏聊天视图不会注销前端，重载窗口在宽限期内可以重新连接。
- 前端每 10 秒发送心跳，45 秒未续约会被视为失效；TCP 断开会立即释放租约。自动 core 启动后 60 秒没有任何前端登记，也会回收。状态栏显示“随前端自动退出”或“手动常驻”。
- `uv run lite core start` 手动启动常驻 core，或者将现有自动 core 转为常驻。直接运行 `lite-core` 默认常驻；`uv run lite core stop` 可主动停止。常驻 core 不随前端退出而停止。
- 点击“日志”打开插件启动日志或默认 core 日志 `~/.agentlite/logs/core.log`；自定义日志位置以 core 配置为准。启动诊断位于扩展的 globalStorage 目录下 `core-launch.log`。

只有受信任的本地文件夹支持启动。第一版不支持 Remote / WSL / 虚拟工作区，不提供历史会话列表、文件引用、diff 审阅、模型切换或 Marketplace 发布。
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

打包输出 `agentlite-vscode-0.0.7.vsix`。在 VS Code 扩展面板菜单选择“从 VSIX 安装”，或运行：

```powershell
code --install-extension ./agentlite-vscode-0.0.7.vsix
```

从旧版升级后，如果 VS Code 记住了左侧位置，右键 AgentLite 图标或视图标题，选择“移动到” → “辅助侧边栏”（Move To → Secondary Side Bar）。VS Code 会记住新位置。默认右侧贡献点从 VS Code 1.106 起正式支持，参见 [官方发布说明](https://code.visualstudio.com/updates/v1_106#_view-containers-in-secondary-side-bar)。

真实模型手工验收：打开侧边栏，让 Agent 读取工作区文件并总结，继续追问，然后请求写入一个临时文件并确认权限，最后取消一个进行中的任务，确认还能继续对话。
UI 自动测试使用 DOM 环境；实际 VS Code 布局、焦点和主题效果仍应在开发窗口中检查。
