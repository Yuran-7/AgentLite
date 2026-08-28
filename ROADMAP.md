# AgentLite Roadmap

## P0：Resume Session — TUI 方案已实现

每个 session 支持一个自然语言名称，方便用户识别和选择。

### 方案一：启动时恢复

```bash
lite-tui resume [--all]
```

- 默认选择当前工作目录下的历史 session。
- `--all` 显示所有工作目录的历史 session。
- 如果选中的 session 不属于当前工作目录，询问用户使用：
  1. session 原来的工作目录；
  2. 当前工作目录。

### 方案二：TUI 内恢复

正常运行 `lite-tui` 后，通过 `/resume` 选择历史 session。

- 列出所有 session，并显示自然语言名称和工作目录。
- 如果选中的 session 不属于当前工作目录，同样询问使用原工作目录还是当前工作目录。

两种方案可以先实现一种，底层共用同一套 session 列表、恢复和工作目录切换能力。

已实现 `/resume`、`session.list` 和 `session.resume`。启动时的
`lite-tui resume [--all]` 入口仍可在后续直接复用这些底层能力。
