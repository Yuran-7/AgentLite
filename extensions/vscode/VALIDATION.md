# Demo 验证记录

日期：2026-10-01，Windows，Node.js 24.19.0，Python 3.12.10。

| 检查 | 结果 |
| --- | --- |
| `npm run check` | TypeScript 类型检查通过 |
| `npm test` | 23 项通过，无跳过；包含独立 core 配置目录、跨项目 Python 缓存、启动提前退出、真实 Python core、前端租约与自动停止 |
| `npm run test:live` | 初版真实模型验收通过读文件、追问、写入权限、取消后继续对话；本次生命周期改动未重复请求真实模型 |
| Python 回归 | 67 项通过：TUI 自动启动与共享退出、CLI 常驻提升、租约边界、TUI、取消、协议、TCP 与原有 IPC 关闭回归 |
| `ruff check extensions/vscode/tests/fixtures` | 通过 |
| `npm run package` | 生成 `agentlite-vscode-0.0.7.vsix`；只包含运行资源、说明和许可证 |
| 实际 VS Code 扩展宿主 | 未完成：本机 VS Code 因 `vscode-updating` 更新锁拒绝启动测试窗口 |

真实模型验收使用独立临时工作区、会话和内存权限策略，禁用 MCP，不改变用户的项目文件或权限缓存。正常 `npm test` 不使用外部模型。

实际 VS Code 验证可在更新结束后重跑：设置 `VSCODE_EXECUTABLE` 后执行 `npm run test:vscode`，或者选择 **AgentLite: VS Code Demo** 按 F5。
自动化 DOM 测试覆盖打包页面脚本、CSP、内容转义、键盘输入、权限按钮、折叠与快照恢复；像素布局、焦点体验和实际主题仍需在窗口中检查。

Python 回归命令：

0.0.3 修复 Windows 后台启动控制台弹窗：真实自动启动测试新增 Windows 控制台可见性检查，确认启动器退出后 core 仍可 ping；MCP、shell 无窗口子进程及相关 Python 回归共 24 项通过。启动采用 `CREATE_NO_WINDOW`，不再使用 Windows Node detached 控制台；此改动需重启旧 core 才生效。

0.0.4 新增前端托管生命周期：两个登记前端共用一个 core，显式注销与异常 TCP 断线均释放租约；最后一个前端离开后 15 秒自动清理退出，普通查询连接不能阻止退出。另验证心跳失效、宽限期重连、无人登记的启动回收，以及手动 CLI 将现有 core 提升为常驻模式。新增模块与修改的应用通过 mypy，相关改动通过 Ruff，协议文档已重新生成。原有 IPC 关闭测试使用隔离模型配置通过；Windows 测试启动等待从 10 秒增加至 30 秒，与前端启动期限一致。

0.0.6 修复跨项目启动：Python 预检返回安装来源和独立 core 配置目录，缓存已验证的解释器路径；core 启动目录与会话 workspace_root 分离。自动候选缺少 AgentLite 时回退，显式解释器错误不静默替换。生产启动测试在目标项目放置无效 .env，验证 core 仍正常启动，session 仍指向目标项目。另用真实 `Repo/claude-code` 项目与现有模型配置启动独立测试端口，连接、前端登记及 session workspace 验证通过，未请求模型；测试实例已关闭。缺少凭证导致子进程提前退出时立即显示可识别原因，不再等待完整启动期限。

0.0.7 增加跨 VS Code 进程的启动锁，并将预检改为轻量模块发现。真实测试同时启动三个独立 Node 客户端，断言一个启动、两个复用，启动日志只出现一次 core PID；继续验证后台存活、跨项目配置、无窗口与最后一个前端离开后自动退出。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/test_tui_app.py tests/unit/test_app_cancel.py tests/unit/test_commands_events.py tests/unit/test_ipc_broadcaster.py tests/unit/test_socket_client.py tests/unit/test_socket_server.py -q
```
