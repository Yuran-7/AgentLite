# AgentLite 上下文管理评审与重构建议

日期：2026-10-02  
评审基线：当前工作区，Git HEAD `9920ae9`，包含尚未提交的模型配置、Runner 和 SessionManager 改动。  
范围：CCB 上下文文档阅读、AgentLite 实现核查、问题验证与重构设计；本次未修改运行时代码。

## 1. 核心判断

AgentLite 已经具备上下文管理的主要零件：会话回放、项目指令、提示词覆盖、工具结果截断、手动与自动压缩、两阶段长期记忆、缓存用量事件、子 Agent 独立上下文。

当前不足主要是这些零件没有共享一套上下文生命周期和预算协议。最值得优先解决的是：

1. **自动压缩与消息持久化不一致**，会漏存压缩后产生的消息，下一轮还会重新加载旧历史。
2. **窗口预算失真且检查太晚**，Anthropic 缓存 token 被漏算，新增工具结果没有进入下一次请求前的预算检查。
3. **历史记录与模型工作上下文混用**，手动压缩重写 thread，记忆提取又读取这份已裁剪的数据。
4. **上下文缺少结构化来源、作用域和有效期**，项目指令、技能、记忆和环境信息主要靠字符串拼接。

建议先修正确性，再建立统一请求组装流程，最后优化召回和缓存。保留现有 Phase 1 / Phase 2 记忆系统，不需要照搬 CCB 的全部实现。

## 2. CCB 文档阅读结论

### 2.1 如何使用这些资料

所给站点的文档主体是对 Claude Code 架构的源码分析；本文将其作为 CCB 提供的设计参考，没有独立验证该站点所描述的全部功能在某个 CCB 发布版本中均已启用。Feature flag、专用 provider、实验模式相关能力不能直接当作通用协议要求。

本次阅读了四篇上下文核心文档和多轮会话文档。下述为简要转述；后续设计是根据 AgentLite 代码提出的方案。

### 2.2 System Prompt：组装、选择、序列化分开

文档把提示词处理拆为内容组装、覆盖优先级选择、provider 分块三个阶段。Section 有缓存生命周期，静态内容与会话动态内容分区，项目与运行时上下文通过明确管道注入。可借鉴的是职责分离及稳定前缀；其全局缓存、归因头和内部开关不应直接移植。[来源：System Prompt 动态组装](https://ccb.agent-aura.top/docs/context/system-prompt)

### 2.3 项目记忆：入口小，详情按需取

文档采用受限入口索引和独立记忆文件，支持相关性筛选、已展示去重、项目范围及记忆过期后的核验。对 AgentLite 最有价值的是让注入层控制相关性和大小，而不是每次放入同一份全局材料；记忆应保留来源并允许被当前证据纠正。[来源：项目记忆系统](https://ccb.agent-aura.top/docs/context/project-memory)

### 2.4 压缩：分层减负，保留连续性

文档描述局部工具结果清理、基于已有会话记忆的压缩、模型摘要回退，并强调压缩边界、近期消息保留、工具调用与结果配对、压缩后上下文重建和超长请求降级。已有会话记忆的复用不等于记忆生成免费，也不能据此保证摘要无信息损失。[来源：上下文压缩](https://ccb.agent-aura.top/docs/context/compaction)

### 2.5 预算：完整请求计数与输出预留

文档区分粗估和精确计数，根据模型窗口和输出预留决定压缩，并设置失败熔断。其阈值示例存在算式与文字不一致：`200K - 20K - 13K = 167K`，不能同时把同一算式解释成 180K。应借鉴预算方法，通过 AgentLite 实际请求和测试决定数值。[来源：Token 预算管理](https://ccb.agent-aura.top/docs/context/token-budget)

### 2.6 多轮会话：状态超出消息数组

文档中的会话编排器还维护用量、文件状态和已加载上下文等信息，并通过 transcript 恢复会话。AgentLite 可借鉴会话状态与单次执行状态的区分，使重启、下一轮和压缩后的行为遵循同一份可恢复状态。[来源：多轮对话管理](https://ccb.agent-aura.top/docs/conversation/multi-turn)

## 3. AgentLite 当前数据流

关键源码链接均为相对路径，行号在下文另行标注，便于仓库内查看。

```text
SessionManager.send_message
  ├─ 读取全局 memory_summary.md
  ├─ 追加用户消息到 thread.jsonl
  ├─ 首次发送时调度历史记忆扫描
  └─ 解析 /skill：覆盖提示词、过滤工具
       ↓
AgentRunner.run_and_capture
  ├─ SessionStore.read_messages：全量回放 → 裁掉未配对尾部 → 截断工具结果
  ├─ 加载工作区根 AGENT.md
  ├─ 创建 ExecutionContext，记录 prefill_len
  └─ AgentLoop
       ├─ system = base/override + AGENT.md + workspace + memory
       ├─ 调 provider → 追加 assistant → 执行工具 → 追加结果
       └─ tool_use 后按上一次响应的 context_pct 判断自动压缩
            ↓
run 结束：context.persistence_messages(prefill_len) → 追加 thread.jsonl

手动 compact：read_messages → 模型摘要 → 备份并重写 thread.jsonl

长期记忆：历史 read_messages → 清洗截断 → Phase 1 rollout summary
          → Phase 2 staging 整理 → MEMORY.md + memory_summary.md
```

### 3.1 应当保留的已有能力

| 已有能力 | 实现依据 | 重构时的处理 |
| --- | --- | --- |
| 多轮持久化与恢复 | [session/store.py](../src/agent_lite/core/session/store.py)、[runner.py](../src/agent_lite/core/runner.py) | 保留 JSONL 与旧 session 兼容性 |
| 手动、自动摘要 | [compact/compactor.py](../src/agent_lite/core/compact/compactor.py)、[loop.py](../src/agent_lite/core/loop.py) | 改成统一压缩计划和提交协议 |
| 工具结果截断 | [compact/budget.py](../src/agent_lite/core/compact/budget.py) | 接入实时请求投影，保留原文 |
| Thinking 原样保存 | [llm/provider.py](../src/agent_lite/core/llm/provider.py)、[loop.py](../src/agent_lite/core/loop.py) | 增加压缩边界与 provider 协议验证 |
| 两阶段长期记忆 | [memory/pipeline.py](../src/agent_lite/core/memory/pipeline.py)、[memory/phase2.py](../src/agent_lite/core/memory/phase2.py) | 保留隔离整理、发布校验、Git baseline |
| 记忆证据与不可信数据规则 | [phase1_prompt.md](../src/agent_lite/core/memory/phase1_prompt.md)、[phase2_prompt.md](../src/agent_lite/core/memory/phase2_prompt.md) | 沿用并补充可执行的作用域过滤 |
| 缓存用量和压缩事件 | [llm/types.py](../src/agent_lite/core/llm/types.py)、[bus/events.py](../src/agent_lite/core/bus/events.py) | 区分费用指标、实际窗口占用和估算 |

现有记忆系统的详细设计见 [memory-phase1-phase2-design.md](memory-phase1-phase2-design.md)。SQLite 中的旧原子记忆表保留了 scope、置信度等字段，但新版 Phase 1 不再向其写入；不能据此认定当前文件记忆链路已经具备同等检索与过滤能力。

## 4. 当前不足及证据

优先级：P0 表示数据正确性或请求可执行性问题；P1 表示此次重构应建立的基础能力；P2 表示后续优化。

### 4.1 P0：自动压缩后持久化游标失效【已复现】

**代码依据：** `runner.py:227` 保存历史长度，`:313` 在 run 结束时按该长度切片；`compactor.py:83` 将消息列表替换为两条摘要消息；`context.py:70` 按数组索引生成持久化增量。

**实际后果：** 当 prefill 有 6 条，压缩后只有 2 条，随后生成最终答复使列表变为 3 条，`messages[6:]` 为空。本地模拟的 run 返回了 `FINAL_AFTER_COMPACT`，thread 仍只有原来的 6 条，最终答复未落盘。摘要文件存在，但下一轮 `read_messages` 不读取它，仍回放旧 thread。

当压缩后的消息再次增长到旧索引以上时，也可能只保存后半段，造成记录缺失或配对异常。另外 `message_metadata` 以索引为键，压缩后没有重建，可能将旧通知元数据误绑定到新消息。

**建议：** transcript 使用稳定消息 ID/序号；持久化增量独立于模型视图。压缩只改变 active view，并写入带覆盖区间的 checkpoint，禁止用可变列表长度充当日志游标。短期修复也必须同时保存本轮原始消息和新的压缩状态，不能只把游标重置为 0，否则旧历史仍会在下一轮回流。

### 4.2 P0：Anthropic 窗口占用漏算缓存 token【已复现】

**代码依据：** `llm/provider.py:141` 使用 `usage.input_tokens / window`，虽然读写缓存字段已经取出，却没有计入占用。

Anthropic 官方说明总输入为三个独立字段之和：`input_tokens + cache_read_input_tokens + cache_creation_input_tokens`。缓存减少费用和重复处理成本，不释放上下文空间。[来源：Anthropic Prompt caching，用量字段说明](https://platform.claude.com/docs/en/build-with-claude/prompt-caching#tracking-cache-performance)

**模拟结果：** 未缓存 10,000、缓存读取 160,000、缓存写入 5,000，代码报告 5%，实际输入占固定 200,000 窗口的 87.5%。这里尚未计算下一轮输出预留和新增消息。

**建议：** provider 负责把原始 usage 归一化成 `total_input_tokens`，保留各费用字段；预算模块使用归一化总量。OpenAI 兼容接口应按其字段定义处理，不能机械叠加 cached 子字段导致重复计数。

### 4.3 P0：预算检查在请求之后，缺少超长恢复【代码确认】

**代码依据：** `loop.py:52` 的循环在调用模型前没有预算检查；`:153` 之后只有 run 仍继续、stop reason 为 tool_use、usage 存在且比例达标时才压缩。使用的是生成工具结果之前的输入占用。

**触发场景：** 历史已经接近窗口，新一轮用户输入、任务通知或一次大文件读取增加大量内容；下一次请求可能直接超长。若上一轮 end_turn，就不会自动压缩；usage 缺失时同样没有估算兜底。异常统一结束为 `llm_error`，没有针对 context overflow 的恢复。

`CompactionConfig.auto_threshold` 默认是 0，Runner 会把此值传入 Loop，因此项目默认禁用自动压缩，不能把 Loop 构造参数的 0.80 当作默认产品行为。

**建议：** 每次 provider.chat 前对实际请求做预算准入；超长错误应识别为独立类型，执行一次受控减负后重试，保留最新用户要求及工具对。摘要请求自身也必须检查预算，并限制重试次数。

### 4.4 P0：工具截断配置没有接入运行路径【代码确认】

**代码依据：** `config.py` 接受 `tool_result_limit/keep` 及环境变量，但当前唯一截断调用是 `session/store.py:177` 的 `truncate_tool_results(messages)`，使用函数默认常量。全局搜索未发现运行路径传入配置值。

同一 run 内 `ExecutionContext.add_tool_result` 直接加入原文，不执行这次截断。ReadFile 的 512 KB 字节上限只限制文件工具，并不能代替统一 token 预算。当前保留前缀的策略也容易丢失日志末尾的失败信息。

**建议：** 完整工具结果先存储；投影到模型时按配置和工具类型生成摘要、头尾片段、原文引用。校验 `keep <= limit`。将该策略同时用于实时执行和历史恢复，而不是仅用于回放。

### 4.5 P1：原始历史、工作上下文与记忆输入混在一起【代码确认】

**代码依据：** 手动压缩 `session/manager.py:638` 调用 `write_compacted`；`session/store.py:224` 备份后重写 thread。历史查询只读当前 thread；记忆扫描 `session/manager.py:511` 也调用已经裁剪和截断的 `read_messages`。

**问题：** 备份保留了旧文件，但没有作为可查询 transcript 或记忆来源接回流程。手动压缩之后，“完整会话”实际上变成模型摘要及其后续消息，可能丢失用户原话、完成证据与来源。run 内消息目前在结束后才批量写入 thread；进程异常退出还可能留下事件日志而缺失对应消息。

**建议：** 分开 append-only transcript、active context checkpoint、UI history、memory extraction input。先定义持久化消息和事件的提交关系，在完整工具轮次结束后保存安全边界；恢复时对中断轮次显式标记，不默默当作完整执行。

### 4.6 P1：摘要全量替换，缺少保留窗口和收益校验【代码确认】

**代码依据：** `compactor.py` 把所有历史转换为纯文本，只检查摘要非空，然后替换成摘要与固定确认。原 token 数采用字符数除以 4；summary token 数采用输出 usage；两者并非严格同口径。没有检查新请求是否确实更小。

**风险：** 最近用户纠正、精确错误、任务通知与工具输出只能依赖模型转述；重复摘要会累积损失。转换器不包含 thinking 等其他 block，也没有显式记录这种裁剪。`context.compacted` 事件不含覆盖序号与新 checkpoint，不能用于恢复 active view。手动路径没有发布与自动路径同等的压缩事件。

**建议：** 压缩旧前缀，保留近期完整轮次；摘要至少保存目标、最新约束、完成证据、当前文件状态、剩余任务和待确认事项。把计划状态作为独立 session 状态恢复。提交前用同一计数器比较完整新旧请求，检查工具对、摘要状态和可用预算；无收益时回退原视图。

当前压缩 prompt 要求保留 IDs、tokens、config 等 Critical Data；这一运行摘要链路未沿用记忆链路的脱敏规则。应区分可保留的标识符与密钥，让摘要保留凭据引用位置而非复制秘密。

### 4.7 P1：模型配置不足以支撑预算【代码确认】

**代码依据：** Anthropic 只有少量窗口映射，未知模型回退 200K；OpenAI 兼容 provider 统一使用 `_DEFAULT_CONTEXT_WINDOW = 200_000`。两个 provider 的输出上限都固定为 8192。

当前 [llm/settings.py](../src/agent_lite/core/llm/settings.py) 的模型字段白名单只有 `id/name/model/protocol/baseUrl/apiKeyEnv`，不支持窗口与输出能力配置。模型可以切换，但预算参数没有同步切换。

**建议：** 模型 profile 增加可配置能力：context window、max output、默认输出预留、tokenizer/count API 可用性及缓存能力。未知兼容端点优先要求本地显式配置，保守回退时标记估算。扩展后同步更新 VS Code settings schema；不要现在直接添加字段，当前加载器会拒绝未知字段。

### 4.8 P1：Prompt 是扁平字符串，覆盖语义容易漂移【代码确认】

**代码依据：** `context.py:36` 将 base/override、项目文件、工作区和长期记忆串接；provider 协议只接受 `system: str | None`。默认规则在 Loop 与两个 provider 中分别维护。Anthropic 把合并字符串放入一个缓存 block，工具列表末尾另设缓存点。

**问题：** 有缓存机制，但不能独立描述内容来源、信任、生命周期和变更原因。记忆在 turn 开始读一次，run 内稳定；下一 turn 若背景整理发布新内容，就会改变整个 system block。缓存是完整前缀匹配，不是给每一段加 cache_control 后各段就能独立命中。

**建议：** 用有序 `PromptSection` 表达基础规则、角色、项目指令、技能、环境快照、记忆。定义 replace 与 append 的精确含义，把通用运行规则单独维护；缓存计划由 provider 适配器生成，能力不支持时退化成普通消息，保留确定顺序。

### 4.9 P1：项目指令范围窄，Skill 参数进入上下文的路径不完整【代码确认】

`memory/loader.py` 只读取工作区根的单数 `AGENT.md`，没有用户级、祖先目录与子目录规则加载，也没有大小预算或内容版本。是否兼容 `AGENTS.md` 应成为显式设计决定。

另一个相关问题：`send_message` 把渲染后的 skill 文本赋给 goal，但 system override 仍是未渲染 template；Runner 有历史时优先回放 thread，最新用户消息仍是原始 `/skill arguments`。因此展开文本并没有直接进入模型消息，模板中的 `$ARGUMENTS` 也可能保留原样。

**建议：** 明确全局、项目和目录指令优先级；访问文件前按目标路径加载适用规则，并以路径、hash 去重。Skill 展开成为专门的上下文项，参数渲染一次，并记录激活与结束；明确它只作用本轮还是跨轮继续，压缩后按同一规则恢复。

### 4.10 P1：长期记忆有作用域标注，但注入和召回没有执行过滤【代码确认】

Phase 2 prompt 要求详细记忆按 scope/keywords/source rollouts 组织，且已有“当前证据优先”的规则，这些设计应保留。

但 `format_memory_summary` 只按文件版本与 10,000 字符限长读取，没有 workspace/query 参数。主任务注入的是同一份 summary，包含跨项目索引；根任务工具注册表也没有接入新版 MEMORY.md 的专用检索入口。旧 `memory.search/delete` 操作的是 SQLite 原子记录，不能直接覆盖新文件产物。

**建议：** 注入“短全局偏好 + 当前项目索引”，按目标和工具轨迹检索详细 task group；给模型只读 search/read 入口，并保留引用。忘记/删除操作要覆盖 rollout、派生记忆和注入产物，建立重新整理或撤销关系，避免旧事实继续留在全局 summary。

### 4.11 P1：记忆前缀截断会使尾部变化永久不可见【已复现】

**代码依据：** `pipeline.py:98` 从最早消息开始分配 60,000 字符总量；`:181` 对截断后的内容计算 source hash。扫描阶段也使用同样的哈希。

**模拟结果：** 第一条字符串消息恰好填满 60,000 字符，第二条由 `OLD_TAIL` 改为 `NEW_TAIL`，两个 source hash 相同。这不是哈希碰撞，而是后面的内容根本没有进入哈希。真实长会话可能漏掉最终结果、纠正和验证，之后又因“已处理”被跳过。

**建议：** 以完整脱敏 transcript 的流式 hash/最终消息序号判定变化；模型输入截断另行记录 coverage。较长会话采用分块或增量摘要，明确 `covered_until_message_id`。对无信号的有效空摘要、模型 JSON 无效、请求失败分别记录状态；目前无效输出会转为空 RolloutSummary，随后可能记为 current，不利于重试。

### 4.12 P2：子 Agent 的上下文策略和主任务不同【代码确认】

`subagent/tool.py:176` 创建新上下文，只继承项目指令和 workspace，不传父会话历史或 memory context；child Loop 没有 compactor。独立上下文本身合理，但长任务没有相同预算保障，继承范围也没有显式模式。

**建议：** 优先保留默认独立模式，提供明确的 `none/task_summary/selected_messages` 继承策略。子任务仍使用统一预算和工具配对检查，结果回传有大小上限、证据与原文引用。完整 fork 可后置。

## 5. 建议的目标结构

### 5.1 分开四类状态

| 状态 | 内容 | 持久化与更新方式 |
| --- | --- | --- |
| Transcript | 用户、assistant、工具调用与结果及来源元数据 | 追加写入；压缩不删除 |
| SessionState | 模型选择、计划、指令版本、已加载规则、记忆版本 | 会话级保存，恢复时重新核验必要信息 |
| ActiveContext | checkpoint 摘要与覆盖范围、近期消息、原文引用 | 压缩可重建，独立于 transcript 游标 |
| RequestContext | 实际 system、messages、工具 schema、预算与缓存计划 | 每次请求生成，可输出脱敏诊断 |

`ExecutionContext` 继续负责 run 状态，避免让它同时承担所有存储和预算职责。SessionManager 负责会话操作和调度；Runner 负责依赖组装；Loop 通过上下文服务取得合法请求。

```text
Transcript + SessionState + 项目指令 + 相关记忆 + 工具 schema
                              ↓
                       ContextAssembler
                              ↓
                     BudgetPlanner（完整请求）
                              ↓ 超预算
              工具结果减负 → 旧前缀摘要 → 校验并提交 checkpoint
                              ↓
                     ProviderAdapter → 模型
                              ↓
                   原始消息落盘 + 用量归一化
```

### 5.2 最小数据契约

以下是建议字段，不是现有 API，也不要求一次全部引入。

| 对象 | 核心字段 | 用途 |
| --- | --- | --- |
| MessageRecord | message_id、seq、role、blocks、run_id、kind、source | 稳定引用和持久化增量 |
| PromptSection | id、content、source、scope、trust、version、lifecycle | 可追踪组装与明确更新时机 |
| ContextCheckpoint | id、covered_until_seq、summary、retained_from_seq、schema_version | 重启后恢复同一个压缩视图 |
| BudgetReport | window、system/tool/history tokens、output reserve、margin、estimate method | 同口径决定是否准入 |
| CompactionPlan | checkpoint_id、candidate range、retained range、pre/post estimate、reason | 可验证、可回退的压缩提交 |
| MemorySelection | artifact version、scope、source rollouts、coverage、surfaced IDs | 相关性过滤与证据追踪 |

缓存生命周期建议先只分固定基础、会话快照、本轮状态三种。真正每步变化的内容需要说明原因；不要为了“动态”把时间、文件状态或工具列表每次无条件写进前缀。

### 5.3 统一预算准入

```text
request_input = system + tool schemas + projected messages + protocol overhead
required = request_input + output_reserve + safety_margin
准入条件：required <= context_window
```

计数优先使用 provider 的完整请求计数能力；热路径使用本地估算，结合真实 usage 校准。中文、JSON、工具参数和非文本 block 采用不同保守处理，并记录估算方法。usage 描述的是上一次输入，只能作为校准，不能代替下一次请求的计数。

软阈值与硬准入分开：软阈值提前安排减负，硬准入阻止明显不合法请求；摘要服务不递归触发自身。若仅 system 与工具 schema 就超预算，压缩历史没有意义，应报告具体占用或减少无关工具。缓存命中不会减少这里的输入占用。

### 5.4 统一压缩事务

1. 基于已落盘的稳定序号确定旧前缀与保留窗口；先确保边界不切断工具对。
2. 将旧工具大输出替换成可定位引用，保留原文；重新计算请求大小。
3. 仍超预算时，对旧前缀生成摘要，保留近期原始轮次和最新用户消息。
4. 验证摘要成功结束、预算收益、关键结构和工具配对；如输出达到 max_tokens，不能把部分摘要当完整 checkpoint。
5. 写入新 checkpoint，再原子更新 active checkpoint 指针；失败则保持旧指针。
6. 重建项目指令、当前计划、适用 Skill 和相关记忆，以总预算限制恢复量。
7. 手动和自动路径使用同一提交函数，发布包含原因、覆盖范围和计数口径的事件。

先做按需前缀摘要即可。以后引入增量 session summary 时，必须证明覆盖序号足够新，才可复用为压缩摘要；不能拿数小时前的长期 rollout summary 直接继续当前任务。

## 6. 推荐实施顺序

### 阶段 A：正确性修复

- 修复 Anthropic 总输入计算，区分实际输入与费用字段。
- 修复自动压缩的持久化游标与元数据绑定，确保下一轮使用新压缩视图。
- 将工具限长配置接入实时模型投影与恢复流程。
- 增加每次请求前的保守预算检查和明确超长错误处理。
- 为 Phase 1 使用完整脱敏内容的变更标识，区分有效 no-op 与失败。

交付标准：压缩后最终答复可在重启后查询；旧历史不会回流；缓存命中不会使占用率降低；大工具结果不能绕过准入。

### 阶段 B：上下文主干重构

- 建立 MessageRecord、ContextCheckpoint、RequestContext。
- 将 transcript 与 active view 分开，统一手动和自动压缩流程。
- 提取 PromptSection / ContextAssembler，明确 override、append 和 Skill 生命周期。
- 模型 profile 增加窗口和输出能力，并同步配置校验、前端 schema 与测试。
- 接入计划状态和压缩后重建，统一主任务与子任务预算入口。

交付标准：同一 transcript/checkpoint 在下一轮、重启和不同前端下得到一致 active view；合法请求完全由同一组装路径生成。

### 阶段 C：项目与记忆质量

- 完成全局/项目/目录指令加载，预算限长、来源与 hash 去重。
- 新文件记忆按 scope 筛选，提供只读检索入口。
- 长会话记忆分块或增量提取，记录 coverage。
- 建立新产物的删除、纠正、过期和重新整理机制。

交付标准：在项目 A 的任务中不会无条件注入项目 B 的详细事实；末尾用户纠正进入后续记忆；删除后派生 summary 不继续使用被撤销事实。

### 阶段 D：性能优化

- provider 能力驱动的缓存分块、稳定工具顺序和前缀诊断。
- 已展示记忆去重、文件版本缓存和子任务选择性继承。
- 用真实长会话评估质量、压缩延迟和成本，再考虑增量会话记忆与工具按需加载。

不建议第一轮移植全局缓存 scope、内部 feature flags、归因头、夜间整理、多 Agent 完整 fork 或向量数据库。它们并非当前正确性问题的前置条件。

## 7. 验收用例与观测

### 7.1 必须补齐的行为测试

| 场景 | 验收断言 |
| --- | --- |
| 6 条 prefill → 自动压缩 → 最终答复 → 重启 | 原始消息可查询；新 checkpoint 可恢复；最终答复存在 |
| 同一 run 多次压缩 | 稳定序号与通知元数据不漂移，消息没有遗漏或重复 |
| Anthropic 缓存读写占多数 | 占用使用三个输入字段之和，费用字段仍分别保存 |
| 切换较小窗口模型 | 使用新模型预算；发送前减负，不沿用 200K 假设 |
| 大输出与末尾错误 | 模型看到关键头尾和引用；原文完整保存；配置确实生效 |
| 多个 tool_use 共用结果消息 | 保留窗口不切断任何调用/结果配对 |
| end_turn 后再提交大输入、usage 缺失 | 都进入请求前检查，有估算兜底 |
| 摘要空、过长、max_tokens、网络失败 | 不提交损坏 checkpoint，有失败次数限制 |
| checkpoint 写入过程中退出 | 恢复旧或新完整版本，不恢复半写版本 |
| /skill 带参数，随后继续或压缩 | 参数正确渲染，作用期与重建规则一致 |
| 记忆输入超过上限，末尾发生纠正 | source revision 改变，coverage 能推进 |
| Phase 1 无效 JSON 与有效空结果 | 前者可重试，后者按有效 no-op 处理 |
| 跨项目与用户关闭记忆 | 作用域过滤生效；关闭后组装不读取记忆内容 |
| 子任务长工具循环 | 使用同样预算入口，结果引用和大小可控 |

### 7.2 建议的观测字段

- 请求：model/profile、window、输出预留、system/tool/history 占用、估算方法、actual total input。
- 压缩：原因、策略、覆盖区间、保留区间、新旧 checkpoint、前后同口径 token、耗时、失败次数。
- 缓存：读写 token、稳定前缀 hash、变化 section；敏感内容不放进诊断日志。
- 记忆：scope、产物版本、候选/选中项、覆盖序号、是否截断、无效输出与重试状态。

这些信息可扩展现有事件和 trace。若修改 bus 模型，需要验证 TUI、CLI、VS Code 的兼容性。

## 8. 本次验证记录与限制

本次只读取代码并创建此文档；已有工作区改动未修改。没有调用真实模型，没有读取或复制用户密钥，没有验证线上成本和压缩质量。

运行以下现有测试：

```powershell
uv run pytest tests/unit/test_context.py tests/unit/test_context_system_prompt.py tests/unit/test_budget.py tests/unit/test_compactor.py tests/unit/test_session_store.py tests/unit/test_memory_loader.py tests/unit/test_llm_provider.py -q
```

结果：**48 passed**。这些测试证明当前基础行为符合已有断言，不覆盖上述压缩跨轮持久化、缓存占用和长会话尾部变化问题。

另外用临时目录和 stub provider 执行三个本地模拟，未向真实 session 写入数据：

| 模拟 | 实际结果 | 结论 |
| --- | --- | --- |
| 6 条历史，首轮 tool_use 触发压缩，下一步最终答复 | run 返回最终答复；thread 仍 6 条；摘要文件 1 个；最终答复未存 | 自动压缩持久化缺陷确认 |
| 输入 10K、缓存读取 160K、写入 5K，窗口 200K | 报告 0.05；总输入占比应为 0.875 | Anthropic 预算漏算确认 |
| 前缀填满 60K 字符，尾部内容发生变化 | 两次 source hash 相同 | 超出截断范围的变更无法检测 |

其余标注“代码确认”的内容来自静态调用链核查；具体产品影响需在重构时加入验收用例。本文没有将摘要准确率、缓存收益或真实模型质量作为已经测得的结论。
