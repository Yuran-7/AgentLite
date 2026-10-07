# 文件搜索工具

`glob` 查找文件名，`grep` 搜索文件内容；它们替代原来的 `list_dir`。
主 Agent、子 Agent 和计划模式均可使用，默认只读免审批。
搜索结果不替代 `read_file` 的阅读记录，编辑已有文件前仍需读取。

两个工具使用 [ripgrep](https://github.com/BurntSushi/ripgrep)；不需要 GNU grep、Bash 或 Git Bash。
可执行文件依次从 `AGENTLITE_RG`、`~/.agentlite/bin/rg.exe`（Linux/macOS 为 `rg`）、PATH 查找。
缺失时会返回安装提示，不会悄悄切换搜索语义。Windows 可按官方说明安装：

```powershell
winget install BurntSushi.ripgrep.MSVC
rg --version
```

也可以将官网下载的可执行文件放到 `~/.agentlite/bin/`，或在 `~/.agentlite/.env` 设置 `AGENTLITE_RG` 的绝对路径。
安装或修改环境配置后重新启动 core。

模型调用示例：

```json
{"pattern":"**/*.py","path":"src"}
```

```json
{"pattern":"class\\s+AgentLoop","path":"src","glob":"*.py","output_mode":"content","-n":true,"-C":2}
```

`grep` 默认 `files_with_matches`，也支持 `content`、`count`、`-i`、`-A/-B/-C`、语言 `type` 和 `multiline`。
`glob` 默认最多 100 条，`grep` 默认最多 250 条；通过 `head_limit` 和 `offset` 翻页，实际截断时结果给出下一页 offset。
内容模式按路径排序，文件列表按修改时间降序。包含隐藏文件，始终排除 `.git`。
默认遵守 ignore 文件；显式正向 glob 过滤遵循 ripgrep 原生语义，会覆盖匹配文件的 ignore 规则。
正则使用 ripgrep 的默认语法；跨行模式同时启用 dotall。搜索有 20 秒及 16 MiB 输出护栏，可通过缩小路径或模式继续检索。
