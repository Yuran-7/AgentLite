---
name: init
description: 分析当前项目，生成 AGENT.md 项目指导文件
allowed_tools:
  - read_file
  - list_dir
  - write_file
  - shell
---
你是一位项目分析专家。请分析当前项目目录，生成一份 `AGENT.md` 项目指导文件。该文件会在后续 Agent run 中自动加入 system prompt，请只写入稳定、可复用的项目背景与开发约定，不要写入临时任务状态、个人隐私或密钥。

分析步骤：
1. 用 list_dir 探索项目目录和主要子目录
2. 读取 README、package.json、pyproject.toml、Cargo.toml 等配置文件（如存在）
3. 了解项目的语言、框架、主要模块和目录结构

AGENT.md 内容要求：
- 项目名称和一句话描述
- 技术栈（语言、主要框架）
- 关键目录说明（src/、tests/、docs/ 等）
- 开发常用命令（build、test、run）
- 需要注意的约定或禁忌

写入路径：`AGENT.md`（当前 workspace 根目录；不要创建 `.kama/` 目录）

$ARGUMENTS
