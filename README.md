# 环境初始化

## 安装到项目根目录（.venv）
项目开始时只有 `.python-version` 和 `pyproject.toml`：前者指定 Python 版本，后者声明项目信息与依赖。执行：

```bash
uv sync
```

uv 会根据 `.python-version` 选择 Python：本机存在兼容版本时直接使用；不存在时，默认自动下载对应版本，再用它创建 `.venv/`。虚拟环境中的解释器位于 Windows 的 `.venv/Scripts/python.exe` 或 macOS/Linux 的 `.venv/bin/python`。

随后 uv 会解析并锁定 `pyproject.toml` 中的依赖，将依赖同步到 `.venv/`，并生成 `uv.lock`。因此项目会从最初的 `.python-version` 和 `pyproject.toml`，增加 `.venv/` 与 `uv.lock`；前者存放隔离环境，后者记录确定的依赖版本。

## 为什么 VS Code 终端会自动进入虚拟环境

在 VS Code 中打开本项目时，Python 扩展会识别或选中项目的 `.venv/` 作为 Python 环境。默认设置下，新建集成终端时，扩展会自动激活所选环境，让终端中的 `python`、`pip` 等命令使用项目的解释器。Windows PowerShell 中可能会看到 VS Code 自动执行 `.venv\Scripts\Activate.ps1`；这是扩展执行的激活命令，不是 PowerShell 因为进入项目目录而自动执行的。因此，在同一目录手动打开独立的 PowerShell，通常不会自动进入虚拟环境。

这个行为由 VS Code 设置 `python-envs.terminal.autoActivationType` 控制：`command`（默认）在终端打开后执行并显示激活命令；`shellStartup` 在 shell 启动过程中激活；`off` 关闭自动激活。修改设置后，需要新建终端才能生效。若想确认终端实际使用的解释器，可运行 `python -c "import sys; print(sys.executable)"`。

虚拟环境创建时，创建工具会把提示符名称记录在 `.venv/pyvenv.cfg` 的 `prompt` 项中，并把名称写入 `activate.ps1` 等激活脚本。激活时，PowerShell 执行的是脚本，不会重新读取 `pyvenv.cfg` 的 `prompt` 来更新提示符。因此，创建环境后若只修改 `pyvenv.cfg`，两处名称就可能不一致：本项目的配置文件写着 `prompt = AgentLite`，但 `activate.ps1` 中仍是 `kamaclaude`，终端便显示 `(kamaclaude)`。这只是显示名称不一致，不代表使用了另一个 Python 环境。要修改 PowerShell 中显示的名称，需要相应修改或重新生成激活脚本，然后新建终端。

## 安装到本地 Python 环境

如果不使用项目虚拟环境，而要安装到本机 Python 3.12，先退出已激活的虚拟环境，再执行：

使用 uv：

```bash
deactivate
uv pip install --system --python 3.12 -e .
```

`--system` 会忽略项目中的 `.venv/`，`--python 3.12` 指定本地解释器，`-e` 表示源码修改后立即生效。安装完成后可执行 `lite`、`lite-core` 或 `lite-tui`；卸载使用：

```bash
uv pip uninstall --system --python 3.12 AgentLite
```

不使用 uv 时，先用 `python --version` 确认当前是 Python 3.12，再使用 pip：

```bash
deactivate
python --version
python -m pip install -e .
```

对应的卸载命令为：

```bash
python -m pip uninstall AgentLite
```
