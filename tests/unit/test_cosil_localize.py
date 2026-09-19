from __future__ import annotations

import json
from pathlib import Path

from agent_lite.core.events.bus import EventBus
from agent_lite.core.config import AgentLiteConfig
from agent_lite.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from agent_lite.core.runner import AgentRunner
from agent_lite.core.tools.builtin.cosil_localize import (
    CosilLocalizeTool,
    _dependency_candidates,
    build_repository_structure,
)


class _ScriptedProvider:
    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = iter(responses)
        self.tool_schemas: list[list[dict[str, object]]] = []

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self.tool_schemas.append(tool_schemas)
        return next(self._responses)


def _write_sample_repo(root: Path) -> None:
    src = root / "src"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "loader.py").write_text(
        "def load_config(path):\n    return path\n",
        encoding="utf-8",
    )
    (src / "parser.py").write_text(
        "from .loader import load_config\n\n"
        "class Parser:\n"
        "    def parse(self, path):\n"
        "        return load_config(path)\n\n"
        "def normalize(value):\n"
        "    return value.strip()\n",
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_parser.py").write_text("def test_parse():\n    pass\n", encoding="utf-8")


def test_build_repository_structure_caches_python_symbols(tmp_path: Path) -> None:
    _write_sample_repo(tmp_path)

    instance_id, structure, cache_path = build_repository_structure(
        tmp_path, include_tests=False, refresh=False
    )

    assert instance_id.startswith(f"{tmp_path.name}-")
    assert "src/parser.py" in structure
    assert "tests/test_parser.py" not in structure
    parser = structure["src/parser.py"]
    assert [item["name"] for item in parser["classes"]] == ["Parser"]  # type: ignore[index]
    assert [item["name"] for item in parser["functions"]] == ["normalize"]  # type: ignore[index]
    assert cache_path.is_file()
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["instance_id"] == instance_id


def test_dependency_candidates_add_imported_python_module(tmp_path: Path) -> None:
    _write_sample_repo(tmp_path)
    _, structure, _ = build_repository_structure(
        tmp_path, include_tests=False, refresh=False
    )

    expanded = _dependency_candidates(structure, ["src/parser.py"])

    assert expanded[0] == "src/parser.py"
    assert "src/loader.py" in expanded


async def test_cosil_localize_runs_file_and_function_stages(tmp_path: Path) -> None:
    _write_sample_repo(tmp_path)
    usage = UsageStats(input_tokens=10, output_tokens=2)
    provider = _ScriptedProvider(
        [
            LlmResponse(
                stop_reason="end_turn",
                text='{"files": ["src/parser.py"]}',
                usage=usage,
            ),
            LlmResponse(
                stop_reason="end_turn",
                text='{"files": ["src/parser.py", "src/loader.py"]}',
                usage=usage,
            ),
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    ToolCallBlock(
                        id="tool-1",
                        name="cosil_get_method",
                        input={
                            "file_name": "src/parser.py",
                            "class_name": "Parser",
                            "function_name": "parse",
                        },
                    )
                ],
                usage=usage,
            ),
            LlmResponse(
                stop_reason="end_turn",
                text='{"keep": true, "reason": "calls the configuration loader"}',
                usage=usage,
            ),
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[ToolCallBlock(id="tool-2", name="cosil_exit", input={})],
                usage=usage,
            ),
            LlmResponse(
                stop_reason="end_turn",
                text=(
                    '{"locations": {"src/parser.py": '
                    '["class: Parser.parse"]}}'
                ),
                usage=usage,
            ),
        ]
    )
    tool = CosilLocalizeTool(provider, EventBus(), "run-1", tmp_path)  # type: ignore[arg-type]

    result = await tool.invoke({"issue": "configuration parsing fails", "top_k_files": 2})

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["found_files"] == ["src/parser.py", "src/loader.py"]
    assert payload["found_related_locs"] == {
        "src/parser.py": ["class: Parser.parse"]
    }
    assert payload["evidence"][0]["kept"] is True
    assert payload["usage"] == {"prompt_tokens": 60, "completion_tokens": 12}
    assert any(schema for schema in provider.tool_schemas if schema)


async def test_cosil_localize_requires_workspace() -> None:
    provider = _ScriptedProvider([])
    tool = CosilLocalizeTool(provider, EventBus(), "run-1", None)  # type: ignore[arg-type]

    result = await tool.invoke({"issue": "anything"})

    assert result.is_error
    assert "requires a session workspace" in result.content


def test_runner_registers_cosil_tool_for_workspace(tmp_path: Path) -> None:
    provider = _ScriptedProvider([])
    runner = AgentRunner(AgentLiteConfig(), provider=provider)  # type: ignore[arg-type]

    registry = runner._build_registry(
        provider=provider,  # type: ignore[arg-type]
        bus=EventBus(),
        run_id="run-1",
        workspace_root=tmp_path,
    )

    assert registry.get("cosil_localize") is not None
