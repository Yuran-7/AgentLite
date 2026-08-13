from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.config import get_config


def _write_env(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# 功能：验证 .env 文件中的值被正确加载并覆盖内建默认值
# 设计：写 .env 到临时目录并 chdir 进去，清除同名系统环境变量排除干扰，确认 .env 加载路径有效
def test_dotenv_base_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    _write_env(env_file, "KAMA_PORT=9999\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAMA_PORT", raising=False)

    cfg = get_config()

    assert cfg.port == 9999


# 功能：验证系统环境变量的优先级高于 .env 文件中的值
# 设计：.env 写 9999，系统环境变量写 8888，确认最终值为 8888，对应四级优先链的顶层约束
def test_system_env_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    _write_env(env_file, "KAMA_PORT=9999\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAMA_PORT", "8888")

    cfg = get_config()

    assert cfg.port == 8888


# 功能：验证 .env 文件不存在时静默跳过，使用内建默认值（不抛异常）
# 设计：chdir 到空目录，清除系统环境变量，确认 get_config() 不因 .env 缺失而崩溃，默认端口为 7437
def test_missing_env_file_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAMA_PORT", raising=False)

    cfg = get_config()

    assert cfg.port == 7437


# 功能：验证 session 默认写入 daemon 当前工作目录下的 .kama/sessions
# 设计：切换到临时目录并清除覆盖变量，确认默认值为相对路径且不会落入用户主目录
def test_sessions_default_to_current_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAMA_SESSIONS_DIR", raising=False)

    cfg = get_config()

    assert cfg.session.dir == ".kama/sessions"


# 功能：验证 KAMA_SESSIONS_DIR 环境变量可以覆盖 session 存储目录
# 设计：设置 Windows 兼容的相对路径，确认环境层配置直接传入 session 配置对象
def test_sessions_dir_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAMA_SESSIONS_DIR", "workspace/kama-sessions")

    cfg = get_config()

    assert cfg.session.dir == "workspace/kama-sessions"


# 功能：验证 TOML 的 session.dir 可以配置 session 存储目录
# 设计：使用显式 KAMA_CONFIG 隔离全局配置，确认新增 section 能通过严格未知键校验
def test_sessions_dir_toml_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml_path = tmp_path / "kama.toml"
    toml_path.write_text('[session]\ndir = "data/sessions"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAMA_CONFIG", str(toml_path))
    monkeypatch.delenv("KAMA_SESSIONS_DIR", raising=False)

    cfg = get_config()

    assert cfg.session.dir == "data/sessions"


# 功能：验证 .env 中设置的 KAMA_CONFIG 能正确影响 TOML 配置文件的加载路径
# 设计：.env 指向自定义 TOML 文件，TOML 中写入不同端口，确认 .env 在 TOML 加载前被读取（优先级链的正确顺序）
def test_dotenv_before_toml_kama_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toml_path = tmp_path / "custom.toml"
    toml_path.write_bytes(b'[core]\nport = 5555\n')

    env_file = tmp_path / ".env"
    _write_env(env_file, f"KAMA_CONFIG={toml_path}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAMA_CONFIG", raising=False)
    monkeypatch.delenv("KAMA_PORT", raising=False)

    cfg = get_config()

    assert cfg.port == 5555


# 功能：验证同一变量经过完整四级优先链后，最终值为最高优先级来源（系统环境变量）
# 设计：同时设置默认值(7437)/TOML(6000)/.env(7000)/系统环境变量(8000)，确认最终值为 8000，是优先级链的综合正确性验证
def test_priority_chain_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 默认值：7437
    # TOML：6000
    # .env：7000
    # 系统环境变量：8000（最高）
    toml_path = tmp_path / "kama.toml"
    toml_path.write_bytes(b'[core]\nport = 6000\n')

    env_file = tmp_path / ".env"
    _write_env(env_file, "KAMA_PORT=7000\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAMA_CONFIG", str(toml_path))
    monkeypatch.setenv("KAMA_PORT", "8000")

    cfg = get_config()

    assert cfg.port == 8000


# 功能：验证 TOML 可以同时配置 LLM 协议、DeepSeek 模型 ID 和 API 地址
# 设计：通过显式 KAMA_CONFIG 隔离其他配置源，逐字段断言新增 [llm] 配置被严格解析
def test_llm_protocol_toml_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml_path = tmp_path / "kama.toml"
    toml_path.write_text(
        '[llm]\nprotocol = "openai"\ndefault_model = "deepseek-v3"\n'
        'base_url = "https://api.example.com/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAMA_CONFIG", str(toml_path))
    monkeypatch.delenv("LLM_PROTOCOL", raising=False)
    monkeypatch.delenv("LLM_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    cfg = get_config()

    assert cfg.llm.protocol == "openai"
    assert cfg.llm.default_model == "deepseek-v3"
    assert cfg.llm.base_url == "https://api.example.com/v1"


# 功能：验证 LLM 环境变量覆盖 TOML 中的协议、模型和 API 地址
# 设计：为三个字段同时设置不同的 TOML 与环境值，确认既有优先级链对新增配置同样生效
def test_llm_env_overrides_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml_path = tmp_path / "kama.toml"
    toml_path.write_text(
        '[llm]\nprotocol = "anthropic"\ndefault_model = "deepseek-old"\n'
        'base_url = "https://anthropic.example.com"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAMA_CONFIG", str(toml_path))
    monkeypatch.setenv("LLM_PROTOCOL", "OPENAI")
    monkeypatch.setenv("LLM_DEFAULT_MODEL", "deepseek-new")
    monkeypatch.setenv("LLM_BASE_URL", "https://openai.example.com/v1")

    cfg = get_config()

    assert cfg.llm.protocol == "openai"
    assert cfg.llm.default_model == "deepseek-new"
    assert cfg.llm.base_url == "https://openai.example.com/v1"


# 功能：验证未知 LLM 协议在配置加载阶段立即失败
# 设计：通过环境变量注入非法协议，断言错误包含变量名，避免运行到首次 API 调用才暴露问题
def test_invalid_llm_protocol_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KAMA_CONFIG", raising=False)
    monkeypatch.setenv("LLM_PROTOCOL", "unknown")

    with pytest.raises(SystemExit, match="LLM_PROTOCOL"):
        get_config()


# 功能：联网配置与子 agent 能力上限可通过 TOML 显式控制
# 设计：同时验证 provider、抓取上限和工具列表，避免新增配置被严格键检查拒绝
def test_web_and_subagent_tool_policy_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml_path = tmp_path / "kama.toml"
    toml_path.write_text(
        '[agent]\nsubagent_allowed_tools = ["read_file", "bash", "web_search"]\n'
        '[web]\nsearch_provider = "searxng"\n'
        'search_base_url = "https://search.example.com"\nfetch_max_chars = 6000\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KAMA_CONFIG", str(toml_path))

    cfg = get_config()

    assert cfg.agent.subagent_allowed_tools == ["read_file", "shell", "web_search"]
    assert cfg.web.search_provider == "searxng"
    assert cfg.web.search_base_url == "https://search.example.com"
    assert cfg.web.fetch_max_chars == 6000
