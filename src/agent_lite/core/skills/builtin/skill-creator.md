---
name: skill-creator
description: 创建或修改 AgentLite 技能，将用户的任务流程整理为可复用的 SKILL.md
allowed_tools:
  - read_file
  - list_dir
  - write_file
  - edit_file
  - shell
---
根据用户需求创建或修改一个 AgentLite 技能，实际写入文件，并报告技能路径和调用示例。

## 确定范围和位置

- 根据用户提供的目标、输入和期望输出编写技能；信息足够时直接创建，只有影响结果的关键需求缺失时才询问。
- 默认写入当前工作区 `.agentlite/skills/<名称>/SKILL.md`。用户明确要求跨项目使用时，写入用户目录 `~/.agentlite/skills/<名称>/SKILL.md`，先解析实际用户目录并遵守当前文件访问权限。
- 不向 `.codex/skills/`、`.claude/skills/` 或 `.agents/skills/` 写入技能，AgentLite 不扫描这些目录。
- 修改已有技能时先读取原文件及相关资源，保留用户已有约定。创建同名技能前检查用户级、工作区及内置来源；无意覆盖时选择不同名称。
- 只有用户明确要求新增或修改 AgentLite 内置技能时，才修改源码中的 `src/agent_lite/core/skills/builtin/<名称>.md`，沿用该目录的平铺格式。

## 编写技能

名称使用简短的小写英文、数字和连字符，目录或文件名与 frontmatter 的 `name` 一致。用 UTF-8 写入文件，第一行必须为 `---`，格式如下：

```markdown
---
name: explain-code
description: 用户要求解释代码时，说明代码用途、执行流程和关键依赖
---
阅读用户指定的代码及必要依赖，解释主要执行步骤，并用具体输入输出举例。
```

- `description` 简洁说明用途和适用请求，详细步骤放在正文。
- 正文写对任务有帮助的背景、操作步骤和结果要求，避免重复通用常识或扩大用户任务范围。
- 需要接收调用参数时，在正文中使用美元符号紧接大写单词 ARGUMENTS 作为占位符；不包含占位符时，AgentLite 会将参数追加到正文末尾。
- 可选 `allowed-tools` 或 `allowed_tools` 为工具名称列表。在 AgentLite 中，非空列表会限制可用工具，空列表或省略则使用默认工具集合。按实际任务选择当前环境已有的工具，不照搬其他产品的工具名或权限语法。
- AgentLite 当前加载器不处理 `context`、`agent`、`disable-model-invocation`、`user-invocable`、动态命令注入或 `agents/openai.yaml`，不要用这些字段承诺功能。技能通过 `/<名称> 参数` 调用，不承诺自然语言自动触发。
- 仅在确有用途时添加 `scripts/`、`references/`、`assets/`，在正文中说明何时以及如何使用，并明确资源路径相对技能目录。不要生成空目录、未完成模板或无关文档。

## 检查并交付

重新读取生成的文件，检查 YAML frontmatter、名称、描述、非空正文、工具名称及资源路径。能使用 AgentLite 加载器时，验证技能可以解析，且工作区调用解析到预期文件；新增脚本应实际运行验证。

报告创建或修改的文件、技能用途和一个具体的斜杠调用示例。验证受环境限制时说明实际检查范围，不声称已经执行未运行的检查。

## 用户需求

$ARGUMENTS
