# 工具结果文件与模型预览

大型工具返回在当前轮进入模型上下文之前保存到 session 的 `tool-results/<hash>.txt`。文件保存工具实际返回的正文；若工具自身已有截断，例如 read_file 的 512 KiB 读取上限，结果文件不会恢复工具未返回的部分。

模型收到 `<persisted-output>` 提示，包含正文路径、原始大小、头尾预览和按行读取说明。`thread.jsonl` 与工具完成事件中的 `output` 保存同一份模型预览；事件同时保存 `output_path`、`original_chars`、`truncated`，避免在 events.jsonl 中重复保存大正文。

小结果仍直接发送。后续轮次与重启回放复用已经保存的模型内容。旧会话中的长工具结果在回放时生成稳定的文件和预览，旧 thread/events 原文件保持原样。主 Agent 和子 Agent 共用会话结果目录，哈希包括 run_id、call_id 和正文以避免重名。

## 读取正文

`read_file` 支持以下参数，行号从 1 开始，end_line 包含在范围内：

```json
{"path": "C:/.../tool-results/id.txt", "start_line": 50, "end_line": 100}
```

日志可以按字面文本搜索，每次默认最多 50 条匹配，返回原行号及后续读取起点：

```json
{"path": "C:/.../events.jsonl", "search": "\"llm.usage\"", "max_matches": 20}
```

范围与搜索可以组合。正文重新读取后仍经过工具结果预算，不能绕过限量。未指定范围或搜索时保留原来的文件读取行为。按行读取不会裁出半条超长记录，单条记录本身超出读取字节上限时会明确报告省略。

## 配置

```toml
[compaction]
tool_result_limit = 8000              # 单条正文字符数触发阈值
tool_result_keep = 4000               # 头尾预览正文的 UTF-8 字节预算
tool_result_token_limit = 4000        # 单条模型结果的估算 token 上限
tool_result_batch_token_limit = 12000 # 同一步工具结果合计的估算 token 上限
```

token 预算按 UTF-8 字节除以四估算，不是模型 tokenizer 的精确计数。超字符阈值或单条预算均会触发落盘；同一步多个工具共享批量预算，平均分配配额且不超过单条上限。预算计算包含预览的路径、提示等文字，不包含 API 对消息和工具调用本身的封装成本。它不是整段会话的上下文窗口硬准入。

裁剪优先保留约 30% 头部和 70% 尾部，多行内容尽量沿行边界保留；预览是摘录而不是保证结构完整的 JSON 文档。超过批量预算的小结果也会落盘。落盘失败时仍发送有限预览，并明确提示省略内容无法从结果文件取回，不会透传超大正文。

修改后需在任务结束时运行 `uv run lite core stop`，再在 AgentLite 中重连，让新 core 加载代码。无需为此更换 session；旧会话可继续使用。
