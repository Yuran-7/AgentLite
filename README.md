# 环境初始化

## 安装到项目根目录（.venv）
项目开始时只有 `.python-version` 和 `pyproject.toml`：前者指定 Python 版本，后者声明项目信息与依赖。执行：

```bash
uv sync
```

uv 会根据 `.python-version` 选择 Python：本机存在兼容版本时直接使用；不存在时，默认自动下载对应版本，再用它创建 `.venv/`。虚拟环境中的解释器位于 Windows 的 `.venv/Scripts/python.exe` 或 macOS/Linux 的 `.venv/bin/python`。

随后 uv 会解析并锁定 `pyproject.toml` 中的依赖，将依赖同步到 `.venv/`，并生成 `uv.lock`。因此项目会从最初的 `.python-version` 和 `pyproject.toml`，增加 `.venv/` 与 `uv.lock`；前者存放隔离环境，后者记录确定的依赖版本。

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
