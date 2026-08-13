from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from kama_claude.core.config import KamaConfig, WebConfig
from kama_claude.core.runner import AgentRunner
from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.builtin.web_fetch import WebFetchTool
from kama_claude.core.tools.builtin.web_search import WebSearchTool


async def _public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.asyncio
async def test_web_search_returns_structured_untrusted_results() -> None:
    html = """
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">
        Example result
      </a>
      <a class="result__snippet">A useful snippet.</a>
    </div>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "html.duckduckgo.com"
        assert "q=test" in str(request.url)
        return httpx.Response(200, text=html)

    tool = WebSearchTool(WebConfig(), transport=httpx.MockTransport(handler))
    result = await tool.invoke({"query": "test", "max_results": 3})

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["provider"] == "duckduckgo"
    assert payload["external_content"]["untrusted"] is True
    assert payload["results"] == [
        {
            "title": "Example result",
            "url": "https://example.com/a",
            "snippet": "A useful snippet.",
        }
    ]


@pytest.mark.asyncio
async def test_web_fetch_extracts_readable_html_and_skips_active_content() -> None:
    html = """
    <html><head><title>Example page</title><script>ignore()</script></head>
    <body><nav>menu</nav><main><h1>Hello</h1><p>Useful content.</p></main></body></html>
    """

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=html,
            headers={"content-type": "text/html; charset=utf-8"},
        )

    tool = WebFetchTool(
        WebConfig(),
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )
    result = await tool.invoke({"url": "https://example.com/page"})

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["title"] == "Example page"
    assert "# Hello" in payload["content"]
    assert "Useful content." in payload["content"]
    assert "ignore" not in payload["content"]
    assert "menu" not in payload["content"]
    assert payload["external_content"]["untrusted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "http://user:secret@example.com/",
    ],
)
async def test_web_fetch_blocks_unsafe_destinations(url: str) -> None:
    async def should_not_run(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("blocked URL must not reach the HTTP transport")

    tool = WebFetchTool(WebConfig(), transport=httpx.MockTransport(should_not_run))
    result = await tool.invoke({"url": url})

    assert result.is_error
    assert result.error_type == "permission_denied"
    assert "Blocked unsafe URL" in result.content


@pytest.mark.asyncio
async def test_web_fetch_revalidates_redirect_target() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    tool = WebFetchTool(
        WebConfig(),
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )
    result = await tool.invoke({"url": "https://example.com/start"})

    assert result.is_error
    assert result.error_type == "permission_denied"
    assert calls == ["https://example.com/start"]


@pytest.mark.asyncio
async def test_web_fetch_enforces_server_side_character_cap() -> None:
    config = WebConfig(fetch_max_chars=500)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 2_000, headers={"content-type": "text/plain"})

    tool = WebFetchTool(
        config,
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )
    result = await tool.invoke({"url": "https://example.com/large", "max_chars": 5_000})

    payload: dict[str, Any] = json.loads(result.content)
    assert payload["truncated"] is True
    assert len(payload["content"]) < 550


def test_root_registry_registers_web_tools_and_honors_empty_whitelist(tmp_path: Path) -> None:
    runner = AgentRunner(KamaConfig(), runs_dir=tmp_path)
    task_manager = TaskManager(tmp_path / ".tasks")

    normal = runner._build_registry(task_manager)
    denied = runner._build_registry(task_manager, tool_whitelist=[])

    assert normal.get("web_search") is not None
    assert normal.get("web_fetch") is not None
    assert denied.tool_schemas() == []
