# 文件操作与历史恢复

AgentLite 的文件工具共用版本校验、跨进程文件锁和原子提交机制。读取资格按 Agent 上下文隔离，主 Agent 与子 Agent 不共享阅读资格。

## 文本读取与修改

- `read_file(path, start_line=1, end_line=None, search=None, max_matches=50, force=false)`：同版本、同查询的重复读取返回 `file_unchanged`；`force=true` 重发正文。正文最多 512 KiB，不超过 1 MiB 的文本支持去重及修改资格记录。
- `edit_file(path, old_string, new_string, replace_all=false)`：精确替换已读文本。默认唯一匹配；全部替换要求每个匹配都已读到。保留 UTF-8 BOM 和原文件行尾。
- `write_file(path, content)`：创建文件或整文件替换。已有文件必须在当前 Agent 上下文完整读过；多个同版本行范围可以累计。传入的 UTF-8 内容和行尾原样写入。

磁盘读取并不等于模型完整看到了内容。结果进入后续模型请求并得到成功响应之后才登记资格，同批次预生成的读写调用不能借读取直接覆盖文件。大结果首尾预览不提供覆盖资格，搜索不提供整文件覆盖资格。被工具上限裁掉的部分也不能授权修改。局部编辑会验证整个文件的字节版本。

文件版本使用 SHA-256，因此能检测保留时间戳的外部修改，也不会因为只有时间戳变化而误报。修改原文件及新内容均限 1 MiB，拒绝非 UTF-8、非普通文件、多硬链接文件和 PDF。压缩、恢复会话、切换上下文以及回放内容被重新截断都会清空资格。

典型调用：

```json
{"path":"src/main.py","start_line":10,"end_line":30}
```

```json
{"path":"src/main.py","old_string":"timeout = 10","new_string":"timeout = 30"}
```

`read_required` 表示需要补充读取；`file_conflict` 表示版本发生变化，应重新读取和重新生成修改；这些错误不自动重试。`edit_file` 与 `write_file` 共用写操作权限策略。

## 原子提交与历史

每次修改先持久化旧、新字节备份及 pending 记录，再将同目录临时文件写完整并 fsync，最后复核版本并原子发布。创建文件不会覆盖抢先出现的目标。已有文件保留 mode；Windows 同时复制 DACL。提交开始后等待真实结果，避免取消后后台仍继续写入。

历史位于会话目录的 `file-history/`：

```text
file-history/
  blobs/<sha256>        # 原始字节，相同内容只存一份
  changes/<uuid>.json  # 路径、前后版本、权限、调用标识及提交状态
```

默认保留最近 100 次记录，备份预算 256 MiB；超限后清理最旧已完成记录和无引用备份，保留 pending 事务引用。无内容或权限变化不产生记录。核心启动时及访问历史时核对 pending，状态可能变为 committed、aborted 或 uncertain。

在 core 运行时执行：

```powershell
uv run lite history list --session <session-id>
uv run lite history diff --session <session-id> --change <change-id>
uv run lite history restore --session <session-id> --change <change-id> --dry-run
uv run lite history restore --session <session-id> --change <change-id>
```

恢复到某次修改前的字节和权限；撤销创建会删除该文件。恢复要求磁盘仍处于该次修改后的版本，连续修改应从新到旧逐次撤销。恢复自身会产生记录，可再次撤销。运行中的会话拒绝执行恢复。CLI 经核心 RPC 执行，不直接绕过核心写文件。

对应 RPC 为 `file_history.list`、`file_history.diff` 和 `file_history.restore`，参数使用 `session_id`、`change_id`，恢复另有 `dry_run`。

## PDF 分页读取

```json
{"path":"paper.pdf","pages":"1-3","pdf_mode":"both"}
```

页码从 1 开始，支持单页或连续范围，每次最多 5 页。最多 5 页的文档可省略 `pages`，更长文档必须指定范围。`pdf_mode` 为 `text`、`images` 或 `both`，默认 `both`；不能与文本行范围、搜索混用。

PDF 文件限 64 MiB。独立进程使用 PDFium 提取文字、渲染页面，90 秒超时或取消后终止并回收。页面图片最长边 1600 像素，base64 编码后最多 1 MiB。文字受现有工具结果预算约束。扫描页没有文字时返回提示；密码保护和损坏文件返回 `pdf_error`，第一版不提供本地 OCR 或密码输入。

会话 `assets/pdf/<文件哈希>/` 保存固定版本源文件、分页文字和图片缓存。消息、日志及界面只保存资产引用和摘要，在 provider 发送请求前展开图片。Anthropic 使用工具结果内图片，OpenAI-compatible 使用工具结果之后的图片消息。扫描件和图表阅读需要支持图片的模型。PDF 不授予文本修改资格。

## 保证边界

这些机制保护 `read_file`、`edit_file` 和 `write_file`。shell、外部编辑器及第三方 MCP 的修改在后续版本校验时检测，不保证事前备份。AgentLite 进程之间遵守共享锁；不遵守锁的外部进程仍可能在最终校验和替换之间修改目标。原子发布保证完整旧版或新版，不提供跨文件事务。
