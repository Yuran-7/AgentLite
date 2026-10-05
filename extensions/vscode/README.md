# AgentLite

AgentLite 是一款轻量级 AI 编程助手，支持在 VS Code 中进行 AI 对话、编辑代码和调用工具。

作者：Ryan Yu


## 权限与计划模式

输入区盾牌图标展开 Manual、Edit automatically、Auto 三项权限菜单，显示说明与当前项勾选。Plan 通过 `/plan` 进入。模型配置通过 `/settings` 打开。新会话默认 Auto，旧会话保持 Manual，恢复时以 core 返回的字段为准。
Manual 使用原有审批策略；Edit automatically 自动编辑工作区普通文件；Auto 对其他普通操作结合真实用户意图进行模型审批。
受保护路径和高危操作暂停确认，分类器超时或不可用也暂停确认。
Auto 审批卡只提供“允许本次、拒绝本次、始终拒绝”，显示确认原因；模型放行的工具显示 `auto-approved`。

运行中可以切换权限模式，Plan 切换需空闲。进入 Plan 保留此前权限模式，执行计划或退出 Plan 后恢复。
多个客户端通过 `session.mode_changed` 同步两种模式；菜单切换失败时保留原状态。
分类器可用 core 的 `[permission]` 配置关闭，或通过 `classifier_model` 指定完整模型配置 ID。
