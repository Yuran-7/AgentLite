from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_lite.core.config import LlmConfig
from agent_lite.core.llm.settings import model_settings, resolve_model


def write_settings(root: Path, data: dict) -> None:
    directory = root / ".agentlite"
    directory.mkdir(exist_ok=True)
    (directory / "settings.json").write_text(json.dumps(data), encoding="utf-8")


def test_user_profiles_ignore_project_reload_and_keep_secrets_off_wire(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    profile = {"id": "custom", "protocol": "openai", "model": "first",
               "apiKeyEnv": "TEST_MODEL_SECRET", "baseUrl": "https://example.test/v1"}
    with patch("pathlib.Path.home", return_value=home):
        write_settings(home, {"defaultModel": "custom", "models": [profile]})
        write_settings(workspace, {"models": [{**profile, "model": "second"}]})
        (home / ".agentlite/.env").write_text("TEST_MODEL_SECRET=file-secret\n", encoding="utf-8")
        (workspace / ".env").write_text("TEST_MODEL_SECRET=project-secret\n", encoding="utf-8")
        (tmp_path / ".env").write_text("TEST_MODEL_SECRET=cwd-secret\n", encoding="utf-8")
        monkeypatch.delenv("TEST_MODEL_SECRET", raising=False)
        settings = model_settings(str(workspace))
        assert settings["settingsPath"] == str(home / ".agentlite/settings.json")
        assert "file-secret" not in json.dumps(settings)
        resolved = resolve_model(LlmConfig(), None, str(workspace))
        assert resolved.default_model == "first"
        assert resolved.api_key == "file-secret"
        assert "file-secret" not in repr(resolved)
        monkeypatch.setenv("TEST_MODEL_SECRET", "system-secret")
        assert resolve_model(LlmConfig(), "custom", str(workspace)).api_key == "system-secret"
        (workspace / ".agentlite/settings.json").write_text("invalid JSON", encoding="utf-8")
        write_settings(home, {"defaultModel": "custom", "models": [{**profile, "model": "third"}]})
        assert resolve_model(LlmConfig(), None, str(workspace)).default_model == "third"
        assert resolve_model(LlmConfig(), "", str(workspace)).default_model == "deepseek-chat"
        with pytest.raises(ValueError, match="missing"):
            resolve_model(LlmConfig(), "missing", str(workspace))


@pytest.mark.parametrize("data", [
    {"models": [{"id": "a", "protocol": "other", "model": "b"}]},
    {"models": [{"id": "a", "protocol": "openai", "model": "b", "apiKey": "secret"}]},
    {"models": {}, "defaultModel": "a"},
    {"models": [], "defaultModel": "unknown"},
])
def test_invalid_settings_fail_without_echoing_secrets(tmp_path, data):
    with patch("pathlib.Path.home", return_value=tmp_path):
        write_settings(tmp_path, data)
        with pytest.raises(ValueError) as exc:
            model_settings()
        assert "secret" not in str(exc.value)


async def test_metadata_survives_restart_and_rejects_busy_session(tmp_path):
    from agent_lite.core.bus.envelope import HandlerError
    from agent_lite.core.events.bus import EventBus
    from agent_lite.core.session.manager import SessionManager
    from agent_lite.core.session.store import SessionStore

    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: None, EventBus())
    session = await manager.create("chat", workspace_root=str(tmp_path))
    session.run_ids.append("test-run")
    store.append_message(session.id, "user", "hello")
    await manager.update_metadata(session.id, title="Renamed", model_id="custom")
    other = SessionManager(store, lambda: None, EventBus())
    resumed = await other.resume(session.id, str(tmp_path))
    assert resumed.title == "Renamed"
    assert resumed.model_id == "custom"
    async with other._locks[session.id]:
        with pytest.raises(HandlerError, match="busy"):
            await other.update_metadata(session.id, title="Rejected")
    assert store.read_meta(session.id).title == "Renamed"


async def test_core_model_and_rename_handlers_validate_and_persist(tmp_path, monkeypatch):
    from agent_lite.core.app import CoreApp
    from agent_lite.core.bus.envelope import HandlerError
    from agent_lite.core.config import AgentLiteConfig
    from agent_lite.core.events.bus import EventBus
    from agent_lite.core.session.manager import SessionManager
    from agent_lite.core.session.store import SessionStore

    monkeypatch.setenv("HANDLER_MODEL_KEY", "handler-secret")
    store = SessionStore(tmp_path / "sessions")
    app = CoreApp()
    app._config = AgentLiteConfig()
    app._sessions = SessionManager(store, lambda: None, EventBus())
    with patch("pathlib.Path.home", return_value=tmp_path):
        write_settings(tmp_path, {"models": [{"id": "test", "model": "custom-model",
                       "protocol": "openai", "apiKeyEnv": "HANDLER_MODEL_KEY"}]})
        created = await app._session_create_handler({"workspace_root": str(tmp_path)})
        sid = created.session_id
        session = app._sessions._get_session(sid)
        session.run_ids.append("test-run")
        listed = await app._model_list_handler({"workspace_root": str(tmp_path)})
        assert "handler-secret" not in json.dumps(listed)
        assert listed['defaultModel'] == 'test'
        assert listed['fallbackModel']['name'] == app._config.llm.default_model
        await app._session_model_handler({"session_id": sid, "model_id": "test"})
        await app._session_rename_handler({"session_id": sid, "title": " New name "})
        resumed = await app._session_resume_handler({"session_id": sid})
        assert resumed.model_id == "test"
        assert resumed.title == "New name"
        with pytest.raises(HandlerError):
            await app._session_model_handler({"session_id": sid, "model_id": "unknown"})
        with pytest.raises(HandlerError):
            await app._session_rename_handler({"session_id": sid, "title": "   "})
        assert store.read_meta(sid).model_id == "test"


def test_first_custom_model_is_used_without_default_model_setting(tmp_path, monkeypatch):
    monkeypatch.setenv('FIRST_MODEL_KEY', 'file-secret')
    with patch('pathlib.Path.home', return_value=tmp_path):
        write_settings(tmp_path, {'models': [{'id': 'first', 'model': 'my-model',
                       'protocol': 'openai', 'apiKeyEnv': 'FIRST_MODEL_KEY'}]})
        assert model_settings()['defaultModel'] == 'first'
        assert resolve_model(LlmConfig(), None, None).default_model == 'my-model'
        write_settings(tmp_path, {'defaultModel': '', 'models': [{'id': 'first', 'model': 'my-model',
                       'protocol': 'openai', 'apiKeyEnv': 'FIRST_MODEL_KEY'}]})
        assert resolve_model(LlmConfig(), None, None).default_model == LlmConfig().default_model


def test_literal_key_in_env_reference_is_rejected_without_disclosure(tmp_path):
    with patch('pathlib.Path.home', return_value=tmp_path):
        write_settings(tmp_path, {'models': [{'id': 'first', 'model': 'my-model',
                       'protocol': 'openai', 'apiKeyEnv': 'sk-test-secret'}]})
        with pytest.raises(ValueError) as exc:
            model_settings()
        assert 'sk-test-secret' not in str(exc.value)
        assert 'MY_MODEL_API_KEY' in str(exc.value)


async def test_runner_uses_session_profile_for_main_and_child_providers(tmp_path, monkeypatch):
    from agent_lite.core.config import AgentLiteConfig
    from agent_lite.core.events.bus import EventBus
    from agent_lite.core.llm.types import LlmResponse
    from agent_lite.core.runner import AgentRunner
    from agent_lite.core.session.manager import SessionManager
    from agent_lite.core.session.store import SessionStore

    class Provider:
        async def chat(self, *args, **kwargs):
            return LlmResponse(stop_reason="end_turn", text="done")

    monkeypatch.setenv("RUNNER_MODEL_KEY", "runner-secret")
    config = AgentLiteConfig()
    config.web.enabled = False
    config.trace.enabled = False
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: None, EventBus())
    session = await manager.create("chat", workspace_root=str(tmp_path))
    session.model_id = "test"
    store.append_message(session.id, "user", "hello")
    runner = AgentRunner(config, events_file=tmp_path / "events.jsonl")
    with patch("pathlib.Path.home", return_value=tmp_path):
        write_settings(tmp_path, {"models": [{"id": "test", "model": "chosen-model",
                       "protocol": "openai", "baseUrl": "https://example.test/v1",
                       "apiKeyEnv": "RUNNER_MODEL_KEY"}]})
        with patch("agent_lite.core.runner.create_llm_provider", return_value=Provider()) as factory:
            outcome = await runner.run_and_capture("hello", session=session, store=store)
            assert outcome.status == "success"
            selected = factory.call_args.args[0]
            assert selected.default_model == "chosen-model"
            assert selected.protocol == "openai"
            assert selected.api_key == "runner-secret"
            runner._create_provider("child-model", session)
            child = factory.call_args.args[0]
            assert child.default_model == "child-model"
            assert child.base_url == "https://example.test/v1"
            assert child.api_key == "runner-secret"


async def test_core_can_start_before_model_credentials_are_configured(tmp_path, monkeypatch):
    from agent_lite.core import app as app_module
    from agent_lite.core.config import AgentLiteConfig
    from agent_lite.core.transport.socket_server import SocketServer

    config = AgentLiteConfig()
    config.port = 0
    config.trace.enabled = False
    config.session.dir = str(tmp_path / "sessions")
    config.memory.dir = str(tmp_path / "memory.db")
    monkeypatch.setattr(app_module, "get_config", lambda: config)
    monkeypatch.setattr(app_module, "setup_logging", lambda _: None)
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    addresses = []
    original = SocketServer.start

    async def start(server):
        address = await original(server)
        addresses.append(server._server.sockets[0].getsockname())
        return address

    monkeypatch.setattr(SocketServer, "start", start)
    app = app_module.CoreApp()
    task = asyncio.create_task(app.run())
    try:
        async with asyncio.timeout(5):
            while not addresses:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        reader, writer = await asyncio.open_connection(*addresses[0][:2])
        try:
            writer.write(b'{"jsonrpc":"2.0","id":"1","method":"core.ping","params":{"client":"test"}}\n')
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), 2))
            assert response["result"]["server_version"]
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        if app._shutdown:
            app._shutdown.set()
        await asyncio.wait_for(task, 5)


# 功能：验证项目中的密钥不能替代缺失的用户密钥
# 设计：只在工作目录和项目 .env 放密钥，断言解析报错并提示用户目录
def test_project_secret_is_not_used(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_MODEL_SECRET", raising=False)
    with patch("pathlib.Path.home", return_value=tmp_path):
        write_settings(tmp_path, {"models": [{"id": "custom", "model": "test",
                       "protocol": "openai", "apiKeyEnv": "TEST_MODEL_SECRET"}]})
        (tmp_path / ".env").write_text("TEST_MODEL_SECRET=project-secret\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"~/\.agentlite/\.env"):
            resolve_model(LlmConfig(), None, str(tmp_path))
