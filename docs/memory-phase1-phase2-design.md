# AgentLite 长期记忆方案：简化版 Phase 1 / Phase 2

## 1. 文档目标

本文记录 AgentLite 长期记忆的优化方案，参考 Codex CLI `v0.147.0` 的 memories 实现，但不直接照搬其复杂的 Git baseline、全局租约和多层任务调度。

当前 AgentLite 的记忆生成主要位于：

- `src/agent_lite/core/memory/store.py`
- `src/agent_lite/core/session/manager.py`
- `src/agent_lite/core/memory/model.py`

当前实现的主要问题是：

1. 通过字符串匹配识别 preference、fact、decision，语义覆盖范围有限。
2. 只处理用户消息，缺少完整任务上下文、工具结果和最终验证结果。
3. 新候选默认进入 `memory_raw_items`，等待 Phase 2 自动合并，流程不应依赖逐条人工审批。

目标方案是：

```text
完整 session transcript
    -> Phase 1：模型提取原始记忆
    -> raw memory items
    -> Phase 2：模型合并、去重、冲突处理
    -> active memories
    -> 后续 session 检索使用
```

## 2. Codex CLI 的实现参考

参考源码仓库：

```text
C:\Users\HuanZhu\Desktop\Repo\codex-rust-v0.147.0
```

### 2.1 启动入口和后台执行

Codex 在收到用户输入后启动 memories startup task：

```text
codex-rs/app-server/src/request_processors/turn_processor.rs:578
```

它先提交当前用户输入，再调用 `start_memories_startup_task`。因此 memories 不是当前回答的前置步骤。

启动任务实现：

```text
codex-rs/memories/write/src/start.rs:23
```

核心结构是：

```rust
tokio::spawn(async move {
    phase1::run(...).await;
    phase2::run(...).await;
});
```

含义是：整个 memories 流程在后台异步运行；后台流程内部仍然是 `Phase 1 -> Phase 2` 的顺序。

### 2.2 旧 session 的筛选

Codex 不会直接处理当前正在进行的 session，而是扫描 state DB 中之前的 rollout。筛选逻辑位于：

```text
codex-rs/state/src/runtime/memories.rs:133-280
```

重要条件包括：

```sql
threads.memory_mode = 'enabled'
threads.id != current_thread_id
threads.updated_at_ms <= idle_cutoff
```

默认参数定义位于：

```text
codex-rs/config/src/types.rs:46-48
```

当前源码默认值为：

- 每次启动最多处理 2 个 rollout
- rollout 最大年龄 10 天
- 至少空闲 6 小时

因此 Codex 是“每次用户输入都尝试启动后台检查”，但不是“每次 run 都一定调用 Phase 1 模型”。没有符合条件的 rollout 时，任务会快速结束。

### 2.3 Phase 1：单个 rollout 提取

Phase 1 的实现位于：

```text
codex-rs/memories/write/src/phase1.rs
```

加载完整 rollout 的位置：

```text
codex-rs/memories/write/src/phase1.rs:283-324
```

它使用：

```rust
RolloutRecorder::load_rollout_items(rollout_path)
```

然后把过滤后的完整会话交给专门的模型，而不是用关键词判断记忆类型。

Phase 1 输出被限制为严格 JSON：

```text
codex-rs/memories/write/src/phase1.rs:128-146
```

格式为：

```json
{
  "raw_memory": "...",
  "rollout_summary": "...",
  "rollout_slug": "..."
}
```

如果没有长期价值，模型返回空字段：

```json
{
  "raw_memory": "",
  "rollout_summary": "",
  "rollout_slug": ""
}
```

Phase 1 提示词要求模型重点识别：

- 稳定的用户偏好和反复纠正
- 高价值的排障经验、命令和路径
- 已验证的项目事实和技术决策
- 能减少未来用户重复说明的信息

同时排除一次性问题、临时状态、普通知识和未经验证的推测。

提示词位置：

```text
codex-rs/memories/write/templates/memories/stage_one_system.md:28-80
codex-rs/memories/write/templates/memories/stage_one_system.md:222-235
```

### 2.4 Phase 1 使用的模型

Phase 1 的模型选择位于：

```text
codex-rs/memories/write/src/phase1.rs:193-198
```

用户可以通过配置覆盖：

```toml
[memories]
extract_model = "..."
consolidation_model = "..."
```

配置字段定义：

```text
codex-rs/config/src/types.rs:315-318
```

如果没有覆盖，provider 会选择默认模型：

```text
codex-rs/model-provider/src/provider.rs:136-147
```

当前 `v0.147.0` 源码默认标识为：

```text
Phase 1：gpt-5.6-luna
Phase 2：gpt-5.6-terra
```

这些模型调用通常在后台完成，对普通用户是透明的；它们也不一定等于当前对话使用的模型。不同 provider 可以覆盖默认模型，例如：

```text
codex-rs/model-provider/src/amazon_bedrock/mod.rs:138-144
```

### 2.5 Phase 1 的中间存储

Phase 1 不直接写最终 `MEMORY.md`，而是先写入 state DB 的 `stage1_outputs` 表。

表结构：

```text
codex-rs/state/memory_migrations/0001_memories.sql:1-15
```

它保存：

- `thread_id`
- `raw_memory`
- `rollout_summary`
- `rollout_slug`
- `generated_at`
- `usage_count`
- `last_usage`
- Phase 2 选择状态

这个中间层相当于“模型提取出的原始记忆素材”，不是逐条等待用户审批的候选列表。

### 2.6 Phase 2：全局 consolidation

Phase 2 实现位于：

```text
codex-rs/memories/write/src/phase2.rs:46-210
```

主要流程：

1. 获取全局 Phase 2 锁。
2. 选择当前需要处理的 `stage1_outputs`。
3. 同步为 `raw_memories.md` 和 `rollout_summaries/`。
4. 用 Git 计算 memory workspace diff。
5. 没有变化时直接结束。
6. 有变化时启动 consolidation agent。
7. consolidation agent 更新 `MEMORY.md`、`memory_summary.md` 和 `skills/`。

是否启动 consolidation agent 的判断：

```text
codex-rs/memories/write/src/phase2.rs:138-166
```

consolidation agent 的限制配置：

```text
codex-rs/memories/write/src/phase2.rs:311-366
```

它被设置为：

- 不使用 memories
- 不使用 MCP
- 不使用网络
- 不递归使用 collab
- 只允许写 memory root

consolidation 提示词：

```text
codex-rs/memories/write/templates/memories/consolidation.md:111-197
```

### 2.7 Codex 的用户控制方式

Codex 主要提供两个开关：

```text
use_memories
generate_memories
```

相关配置逻辑：

```text
codex-rs/tui/src/app/config_persistence.rs:648-722
```

含义是：

- `use_memories`：当前 session 是否读取已有记忆
- `generate_memories`：当前 session 是否允许作为未来记忆的输入

这不是每生成一条候选就询问用户，而是“会话级授权 + 模型判断 + 后台整理”。

## 3. AgentLite 简化版方案

### 3.1 目标架构

```text
run 完成
    ↓
检查 session 是否允许生成记忆
    ↓
后台调用 Phase 1 模型
    ↓
memory_raw_items
    ↓
后台调用 Phase 2 模型
    ↓
程序验证操作
    ↓
SQLite transaction
    ↓
active memories
```

不建议第一版实现 Codex 的 Git baseline、复杂租约和跨启动选择算法。

### 3.2 Phase 1 触发策略

第一版可以在 run 成功后启动后台任务：

```python
outcome = await runner.run(...)

if outcome.status == "success" and session.memory_generate_enabled:
    asyncio.create_task(
        memory_pipeline.extract_session(session.id)
    )
```

为了避免同一个 session 被重复提取，增加：

```text
last_memory_extracted_run_id
memory_generation_status
memory_source_hash
```

建议初始策略：

```text
一个 session 第一次成功完成 run 后提取一次
```

后续再增加：

- session idle 延迟
- 增量提取
- 应用启动时扫描旧 session
- 失败重试

### 3.3 Phase 1 输入

输入完整 session transcript，而不是只传用户消息：

```json
[
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": "..."},
  {"role": "tool", "content": "..."}
]
```

发送前过滤：

- token、密码、API key
- 大段无关工具输出
- 二进制内容
- 内部系统提示词
- 外部网页中的指令性内容

### 3.4 Phase 1 输出协议

建议使用结构化 JSON：

```json
{
  "should_store": true,
  "summary": "本次会话的主要内容",
  "items": [
    {
      "type": "preference",
      "scope": "global",
      "key": "response.style",
      "content": "用户偏好简洁、直接的技术回答",
      "evidence": "用户多次要求回答简洁",
      "confidence": 0.95,
      "stability": "stable"
    }
  ]
}
```

没有长期价值时：

```json
{
  "should_store": false,
  "summary": "",
  "items": []
}
```

推荐类型：

- `preference`：用户偏好和工作习惯
- `fact`：稳定的用户或项目事实
- `decision`：已经采用的项目技术决策
- `procedure`：经过验证的可复用流程

这些类型应由模型根据上下文判断，不应再由正则表达式决定。

### 3.5 中间表设计

建议新增：

```sql
CREATE TABLE memory_raw_items (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    type TEXT NOT NULL,
    scope TEXT NOT NULL,
    key TEXT,
    content TEXT NOT NULL,
    evidence TEXT,
    confidence REAL,
    stability TEXT,
    summary TEXT,
    source_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    processed_at TEXT
);
```

这里的 `pending` 表示“等待 Phase 2 合并”，不表示必须人工审批。

AgentLite 实现直接使用 `memory_raw_items` 作为 Phase 1 原始结果队列，不再保留人工审批队列。

### 3.6 Phase 2 输出协议

Phase 2 输入：

- Phase 1 原始 items
- 当前 active memories
- workspace 信息
- 必要的历史证据

Phase 2 输出操作列表：

```json
{
  "operations": [
    {
      "action": "update",
      "target_key": "response.style",
      "type": "preference",
      "scope": "global",
      "content": "用户偏好简洁、直接的技术回答",
      "reason": "与已有记忆一致，但证据更充分",
      "confidence": 0.97
    }
  ]
}
```

合法操作：

```text
add
update
delete
skip
```

模型只提出操作，不能直接写数据库。程序必须先校验：

- 操作类型
- memory type
- scope
- 内容长度
- 敏感信息
- confidence 范围
- update 的目标是否存在
- workspace/session 边界

校验通过后再用一个 SQLite transaction 执行写入。

### 3.7 冲突处理

建议通过 `scope + key` 定位同一类记忆。

例如：

```text
scope=global
key=response.style
```

新旧记忆冲突时，让 Phase 2 模型决定：

- 保留旧记忆
- 更新为新记忆
- 合并为条件性偏好
- 废弃旧记忆

旧记录不建议物理删除，可以使用：

```text
active
superseded
deleted
```

### 3.8 读取策略

不要把所有记忆都放进 system prompt。建议使用三层结构：

```text
memory_summary
    始终加载，负责导航

active memories
    根据当前问题搜索 top-K

evidence/history
    只有在必要时读取
```

第一版继续使用 SQLite FTS 即可：

1. 按当前问题搜索。
2. 按 `scope` 过滤。
3. 按 `confidence` 和 `importance` 排序。
4. 限制最大条数和最大字符数。

后续再考虑 embedding。

## 4. 推荐的改造顺序

### 第一步：替换字符串提取

将：

```text
src/agent_lite/core/memory/store.py:_extract_explicit
```

替换为：

```python
MemoryExtractor.extract(transcript)
```

先实现 `MemoryExtractor` 和 `memory_raw_items`，让 Phase 1 结果与 active memories 解耦。

### 第二步：实现 Phase 2

新增：

```python
MemoryConsolidator.consolidate(
    raw_items,
    existing_memories,
)
```

它只返回 add/update/delete/skip 操作，由程序执行实际写入。

### 第三步：增加后台调度

最后再增加：

- session idle 检查
- 应用启动扫描旧 session
- retry 和 backoff
- source hash 去重
- usage_count
- 记忆过期、降级和遗忘

## 5. 最终设计原则

1. 用模型理解完整 session，而不是用关键词识别单句话。
2. Phase 1 负责“从单个 session 提取原始记忆”。
3. Phase 2 负责“跨 session 合并和维护长期记忆”。
4. 用户控制 session 是否可以读取或生成记忆，而不是默认逐条审批。
5. 模型只生成结构化候选或操作，最终数据库写入由程序校验并执行。
6. 所有记忆都必须经过敏感信息过滤、scope 限制、长度限制和幂等处理。
7. 记忆是辅助召回层，项目强制规则仍应放在 `AGENT.md`、文档或代码中。
