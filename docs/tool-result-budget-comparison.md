# 当前轮工具结果限量与持久化：本地源码调查

调查日期：2026-10-02。范围：用户提供的 `Repo/codex-rust-v0.147.0` 和 `Repo/claude-code` 本地快照，及 AgentLite 的对照。只读研究，未修改运行时代码，未运行两个仓库的测试。

`claude-code/AGENTS.md` 自述为反编译／重建版，包含 stub、功能开关和本地改动；下述 Claude 部分是该仓库的实现，不能等同于官方发行版的全部行为。Codex 也按本地快照分析，不用目录名推断所有文件均未经修改。

## 结论

持久化记录、工具原始产物和模型上下文是三个不同对象。保存一个结果，不意味着每次请求都发送它的全文。当前轮大结果应在下一次模型调用之前变成有限长度的模型消息，不能只在下一轮用户提问时裁剪。

| 项目 | Codex 本地快照 | Claude 重建仓库 |
|---|---|---|
| 单条大文本 | 工具自己的输出限制，加模型历史级截断 | 大结果写独立文件，tool_result 变成路径与预览 |
| 生效时机 | 当前轮记录模型历史时；部分工具更早裁剪 | 工具结果构造时；批量处理在模型请求前 |
| 模型看到什么 | 限长内容，通常保留头尾与省略提示 | 小预览、结果大小、文件路径；小结果仍可内联全文 |
| 保存全文是否统一保证 | 否；ResponseItem 可保留历史级截断前的内容，但 handler 和采集层可能已限量 | 否；可保存大文本，仍有失败、类型、子 Agent、禁用持久化和文件大小等例外 |
| 全部结果累计超限 | 上下文预算与自动压缩另行处理 | 可选的单消息批量预算、microcompact、autocompact |

## Codex：存盘与模型历史分开

[`record_conversation_items`](../../Repo/codex-rust-v0.147.0/codex-rs/core/src/session/mod.rs:2969) 接收 ResponseItem 后，先把条目记录到内存 ContextManager，再把传入条目写入 rollout。内存历史的 `record_items` 调用 `process_item` 得到处理后的副本；这一步的截断不会直接修改传给持久化层的原条目。

[`ContextManager::process_item`](../../Repo/codex-rust-v0.147.0/codex-rs/core/src/context_manager/history.rs:344) 对 FunctionCallOutput 和 CustomToolCallOutput 执行截断。该层使用模型的 truncation_policy，并为序列化预算乘以 1.2。因此政策预算不是最终 HTTP 请求字节数或 tokenizer 实测值的绝对硬上限。

[`truncate_text`](../../Repo/codex-rust-v0.147.0/codex-rs/utils/output-truncation/src/lib.rs:25) 根据 Bytes/Tokens 政策进行中间截断，保留头尾，加入省略提示。token 估计使用启发式，不能把字符、字节与实测 token 等同。

[`tool_output_token_limit` 配置映射](../../Repo/codex-rust-v0.147.0/codex-rs/models-manager/src/model_info.rs:38) 覆盖模型 truncation_policy；未配置时采用模型元数据的政策，不能统一声称默认一定是 10000。官方配置参考也将该参数描述为单条工具／函数输出的历史预算：[OpenAI 配置参考](https://developers.openai.com/codex/config-reference)。

持久化规则 [`should_persist_response_item`](../../Repo/codex-rust-v0.147.0/codex-rs/rollout/src/policy.rs:39) 包含 FunctionCallOutput 与 CustomToolCallOutput；但事件层另有白名单，ExecCommandEnd 等瞬时事件并不持久化。因此 rollout 不是“把全部原始 UI 事件永久保存”的日志。

**历史级截断前保存，不等于工具的原始输出完整保存。** 例如 [`ExecCommandToolOutput::model_output_max_tokens`](../../Repo/codex-rust-v0.147.0/codex-rs/core/src/tools/context.rs:408) 使用请求参数与模型政策预算的较小者；普通工具模式输出在形成 ResponseItem 之前已经限量。进程采集自身也有限额：shell 类采集采用 DEFAULT_OUTPUT_BYTES_CAP，本地定义为 1 MiB，可信内部 FullBuffer 路径例外。

该快照还有独立 code-mode 路径：`code_mode_result` 使用自己的 max_output_tokens；为空时返回已经采集到的 raw_output，并未使用普通工具模式的同一 min 逻辑。不能把普通函数工具模式的保证推广到所有执行路径。

在这里核查的普通工具历史处理路径中，没有通用的“截掉的每个结果都写完整文件，并保证模型能从该文件找回”的逻辑。若要确保保留全文，可以让命令显式重定向到文件，再通过搜索或范围读取取得需要的内容。源文件本身仍在时也可直接重新读取。

## Claude 重建仓库：文件正文与模型预览分开

[`processToolResultBlock`](../../Repo/claude-code/src/utils/toolResultStorage.ts:205) 在构造 tool_result 时检查大小。一般工具阈值取声明上限与默认 50000 字符的较小者；实验配置可覆盖；Infinity 可退出这套通用规则，图片也有特殊处理。Bash 声明的阈值是 30000 字符。不能把 constants 中的 100000 token 值误认为所有工具都能内联这么多。

[`persistToolResult`](../../Repo/claude-code/src/utils/toolResultStorage.ts:137) 将大文本保存在会话的 `tool-results/<tool_use_id>.txt` 或 `.json` 文件。然后 [`buildLargeToolResultMessage`](../../Repo/claude-code/src/utils/toolResultStorage.ts:189) 生成模型消息：

```text
<persisted-output>
Output too large (...). Full output saved to: .../tool-results/id.txt

Preview (first 约2KB):
...小预览...
</persisted-output>
```

这不是调用模型生成的摘要，而是确定性的预览。模型可根据路径另行搜索、分段读取；保存文件的动作不会自动将全文注入下一次请求。PREVIEW_SIZE_BYTES 常量为 2000；实际实现与多字节字符要分开看，不能换算为严格的 500 token 上限。

[`toolExecution.ts`](../../Repo/claude-code/src/services/tools/toolExecution.ts:1483) 把处理后的 block 放入 message.content，同时可在 toolUseResult 字段保留 UI 用的数据。子 Agent 默认可能省略该字段。[`query.ts`](../../Repo/claude-code/src/query.ts:541) 构造请求副本时删除 toolUseResult，再做预算、snip、microcompact 等处理，说明展示用数据不应直接变成模型输入。

多工具一起返回还有 [`applyToolResultBudget`](../../Repo/claude-code/src/query.ts:567)：**单个合并 user 消息**中的工具结果合计默认超过 200000 字符时，选择最大的、首次出现的候选结果落盘并替换为预览。这不是整个用户问题／整个 agent loop 的总 token 上限。

这套批量预算由 `tengu_hawthorn_steeple` 功能开关控制，代码默认 false；上限可由 `tengu_hawthorn_window` 覆盖。已经发送过的结果会被冻结，若冻结内容本身超过预算，代码允许超额。因此它也是策略机制，并非所有情况下严格满足上限的硬准入。

[`recordContentReplacement`](../../Repo/claude-code/src/utils/sessionStorage.ts:1531) 将替换决定追加到 transcript，包含 tool_use_id 与模型实际看到的替换文本。恢复逻辑据此重新应用相同文本，不会因为重启又恢复为全文；替换稳定也有利于缓存前缀保持一致。

小结果一般内联保存；大正文可在独立文件保存；会话中还可存在 raw toolUseResult 或 content-replacement 记录。不能承诺所有工具的所有原始输出都完全保留：通用落盘失败时可能返回原 block；MCP 有自己的限量与错误降级；图片非文本有特殊路径；Bash 大文件保存有 64 MiB 上限；会话持久化可被禁用。

对于你遇到的读取大日志问题，Read 工具还会更早拒绝过大读取。其本地 [`limits.ts`](../../Repo/claude-code/packages/builtin-tools/src/tools/FileReadTool/limits.ts:1) 默认总文件大小检查 256 KiB，文本输出检查 25000 token，支持覆盖。[`validateContentTokens`](../../Repo/claude-code/packages/builtin-tools/src/tools/FileReadTool/FileReadTool.ts:753) 在超限时抛错，提示使用 offset/limit 或搜索，而不是把整份日志全文喂给模型。这些默认值属于本地重建版本，不是官方版本通用承诺。

microcompact 是另一层策略：它可以清掉旧工具结果，只保留近期结果；时间触发和缓存编辑路径各自有条件。autocompact 则进一步处理整个上下文。它们不能替代刚收到的大结果的单条／批量预算。

## AgentLite 可借鉴的实现边界

1. events.jsonl 可以保留工具已采集到的结果用于审计，但不应被当作模型工作历史。
2. 结果进入 `context.messages` 前，产生独立的 model_content。大正文写到独立 tool-results 文件；model_content 只含有限预览、结果 ID、大小和路径。
3. 同时检查单条结果与当前请求的总输入预算；多个略小于单条阈值的结果也能共同撑爆上下文。
4. 持久化首次产生的 model_content 及替换决定。当前轮、下一轮、重启回放使用同一内容，不应只在下一轮突然裁剪。
5. 提供范围读取和搜索；再次读取结果文件也必须执行预算，防止把落盘全文重新一次性导入。
6. 文件落盘失败时明确降级，不能把超大原文无条件透传给模型；工具内部 LLM 用量与主 Agent 水位分开统计。

示意流程：`工具采集 → 原始产物存储 → 限量模型结果 → 工作上下文 → 请求总预算检查 → 模型`。磁盘正文可大，模型消息可小；两者由 result_id/call_id 关联。
