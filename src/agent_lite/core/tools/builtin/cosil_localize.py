from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.base import LLMProvider
from agent_lite.core.llm.types import LlmResponse, ToolCallBlock
from agent_lite.core.tools.base import BaseTool, ToolResult

_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
}
_MAX_STRUCTURE_CHARS = 60_000
_MAX_SOURCE_CHARS = 16_000


class CosilLocalizeParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    issue: str = Field(min_length=1, max_length=30_000)
    top_k_files: int = Field(default=5, ge=1, le=20)
    top_k_symbols: int = Field(default=10, ge=1, le=30)
    max_rounds: int = Field(default=6, ge=1, le=12)
    include_tests: bool = False
    refresh_structure: bool = False


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, response: LlmResponse) -> None:
        if response.usage is None:
            return
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _instance_id(root: Path) -> str:
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", root.name).strip("-") or "repository"
    return f"{safe_name}-{digest}"


def _source_segment(lines: list[str], start: int, end: int) -> str:
    numbered = [f"{idx:>6}  {lines[idx - 1]}" for idx in range(start, end + 1)]
    text = "\n".join(numbered)
    if len(text) > _MAX_SOURCE_CHARS:
        return text[:_MAX_SOURCE_CHARS] + "\n[truncated]"
    return text


def _parse_python_file(path: Path) -> dict[str, object]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeError) as exc:
        return {"classes": [], "functions": [], "imports": [], "parse_error": str(exc)}

    classes: list[dict[str, object]] = []
    functions: list[dict[str, object]] = []
    imports: list[dict[str, object]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(
                {"name": node.name, "start": node.lineno, "end": node.end_lineno or node.lineno}
            )
        elif isinstance(node, ast.ClassDef):
            methods = [
                {
                    "name": child.name,
                    "start": child.lineno,
                    "end": child.end_lineno or child.lineno,
                }
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            classes.append(
                {
                    "name": node.name,
                    "start": node.lineno,
                    "end": node.end_lineno or node.lineno,
                    "methods": methods,
                }
            )
        elif isinstance(node, ast.Import):
            imports.extend({"module": alias.name, "level": 0} for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append({"module": node.module or "", "level": node.level})
    return {"classes": classes, "functions": functions, "imports": imports}


def _fingerprint(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(f":{stat.st_mtime_ns}:{stat.st_size}\n".encode())
    return digest.hexdigest()


def _python_files(root: Path, *, include_tests: bool) -> list[Path]:
    result: list[Path] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if name not in _IGNORED_DIRS
            and not (name == ".agentlite")
            and (include_tests or name not in {"test", "tests"})
        ]
        current_path = Path(current)
        for name in names:
            if not name.endswith(".py"):
                continue
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if not include_tests and (
                name.startswith("test_")
                or name.endswith("_test.py")
                or any(part in {"test", "tests"} for part in Path(relative).parts)
            ):
                continue
            if path.is_file() and _path_is_within(path, root):
                result.append(path)
    return sorted(result)


def build_repository_structure(
    root: Path,
    *,
    include_tests: bool,
    refresh: bool,
) -> tuple[str, dict[str, dict[str, object]], Path]:
    root = root.resolve()
    files = _python_files(root, include_tests=include_tests)
    fingerprint = _fingerprint(root, files)
    instance_id = _instance_id(root)
    cache_path = root / ".agentlite" / "cosil" / "repo_structures" / f"{instance_id}.json"

    if not refresh and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fingerprint and isinstance(cached.get("files"), dict):
                return instance_id, cached["files"], cache_path
        except (OSError, json.JSONDecodeError):
            pass

    structure: dict[str, dict[str, object]] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        structure[relative] = _parse_python_file(path)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "instance_id": instance_id,
        "repo_root": str(root),
        "fingerprint": fingerprint,
        "files": structure,
    }
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return instance_id, structure, cache_path


def _render_structure(structure: dict[str, dict[str, object]]) -> str:
    lines: list[str] = []
    for file_name, metadata in sorted(structure.items()):
        lines.append(f"file: {file_name}")
        for cls in metadata.get("classes", []):
            if not isinstance(cls, dict):
                continue
            methods = cls.get("methods", [])
            method_names = [m.get("name", "") for m in methods if isinstance(m, dict)]
            suffix = f" methods={method_names}" if method_names else ""
            lines.append(f"  class: {cls.get('name', '')}{suffix}")
        function_names = [
            fn.get("name", "")
            for fn in metadata.get("functions", [])
            if isinstance(fn, dict)
        ]
        if function_names:
            lines.append(f"  functions: {function_names}")
        if sum(len(line) + 1 for line in lines) > _MAX_STRUCTURE_CHARS:
            lines.append("[repository structure truncated]")
            break
    return "\n".join(lines)


def _extract_json(text: str) -> object:
    candidates = [text.strip()]
    candidates.extend(re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            start = candidate.find(opener)
            end = candidate.rfind(closer)
            if start >= 0 and end > start:
                try:
                    return json.loads(candidate[start : end + 1])
                except json.JSONDecodeError:
                    continue
    raise ValueError("model response did not contain valid JSON")


def _file_list(text: str, valid_files: set[str], limit: int) -> list[str]:
    parsed = _extract_json(text)
    values: object
    if isinstance(parsed, dict):
        values = parsed.get("files", [])
    else:
        values = parsed
    if not isinstance(values, list):
        raise ValueError("model response must contain a files array")
    result: list[str] = []
    for value in values:
        path = str(value).replace("\\", "/").strip()
        if path in valid_files and path not in result:
            result.append(path)
        if len(result) >= limit:
            break
    return result


def _module_name(file_name: str) -> str:
    path = file_name[:-3] if file_name.endswith(".py") else file_name
    if path.endswith("/__init__"):
        path = path[: -len("/__init__")]
    return path.replace("/", ".")


def _dependency_candidates(
    structure: dict[str, dict[str, object]], initial: list[str], limit: int = 30
) -> list[str]:
    module_to_file = {_module_name(file_name): file_name for file_name in structure}
    edges: dict[str, set[str]] = {file_name: set() for file_name in structure}
    for file_name, metadata in structure.items():
        current_parts = _module_name(file_name).split(".")[:-1]
        for item in metadata.get("imports", []):
            if not isinstance(item, dict):
                continue
            module = str(item.get("module", ""))
            level = int(item.get("level", 0))
            if level:
                base = current_parts[: max(0, len(current_parts) - level + 1)]
                module = ".".join([*base, module] if module else base)
            parts = module.split(".") if module else []
            for end in range(len(parts), 0, -1):
                target = module_to_file.get(".".join(parts[:end]))
                if target:
                    edges[file_name].add(target)
                    break

    expanded = list(initial)
    initial_set = set(initial)
    for source in initial:
        expanded.extend(sorted(edges.get(source, set())))
    for source, targets in edges.items():
        if targets & initial_set:
            expanded.append(source)
    return list(dict.fromkeys(expanded))[:limit]


def _symbol_outline(structure: dict[str, dict[str, object]], files: list[str]) -> str:
    return _render_structure({name: structure[name] for name in files if name in structure})


def _find_symbol(
    root: Path,
    structure: dict[str, dict[str, object]],
    tool_call: ToolCallBlock,
) -> tuple[str, str]:
    file_name = str(tool_call.input.get("file_name", "")).replace("\\", "/")
    if file_name not in structure:
        raise ValueError("file_name is not one of the candidate files")
    path = (root / file_name).resolve()
    if not _path_is_within(path, root):
        raise PermissionError("candidate path escapes the workspace")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    metadata = structure[file_name]

    if tool_call.name == "cosil_get_class":
        class_name = str(tool_call.input.get("class_name", ""))
        for cls in metadata.get("classes", []):
            if isinstance(cls, dict) and cls.get("name") == class_name:
                key = f"class: {class_name}"
                return key, _source_segment(lines, int(cls["start"]), int(cls["end"]))
    elif tool_call.name == "cosil_get_method":
        class_name = str(tool_call.input.get("class_name", ""))
        function_name = str(tool_call.input.get("function_name", ""))
        for cls in metadata.get("classes", []):
            if not isinstance(cls, dict) or cls.get("name") != class_name:
                continue
            for method in cls.get("methods", []):
                if isinstance(method, dict) and method.get("name") == function_name:
                    key = f"class: {class_name}.{function_name}"
                    return key, _source_segment(lines, int(method["start"]), int(method["end"]))
    elif tool_call.name == "cosil_get_function":
        function_name = str(tool_call.input.get("function_name", ""))
        for function in metadata.get("functions", []):
            if isinstance(function, dict) and function.get("name") == function_name:
                key = f"function: {function_name}"
                return key, _source_segment(
                    lines, int(function["start"]), int(function["end"])
                )
    raise ValueError("the requested class or function was not found")


_LOCATION_TOOL_SCHEMAS: list[dict[str, object]] = [
    {
        "name": "cosil_get_class",
        "description": "Read one complete class from a candidate Python file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string"},
                "class_name": {"type": "string"},
            },
            "required": ["file_name", "class_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cosil_get_method",
        "description": "Read one method from a class in a candidate Python file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string"},
                "class_name": {"type": "string"},
                "function_name": {"type": "string"},
            },
            "required": ["file_name", "class_name", "function_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cosil_get_function",
        "description": "Read one top-level function from a candidate Python file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string"},
                "function_name": {"type": "string"},
            },
            "required": ["file_name", "function_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cosil_exit",
        "description": "Stop retrieving source when enough evidence has been gathered.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


class CosilLocalizeTool(BaseTool):
    params_model = CosilLocalizeParams
    name = "cosil_localize"
    description = (
        "Localize a software issue in the current Python repository using a CoSIL-inspired "
        "pipeline: cached AST structure, LLM file selection, static import expansion and "
        "reflection, then iterative class/function source inspection with relevance pruning."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "issue": {"type": "string", "description": "Bug report or change request."},
            "top_k_files": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            "top_k_symbols": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "default": 10,
            },
            "max_rounds": {"type": "integer", "minimum": 1, "maximum": 12, "default": 6},
            "include_tests": {"type": "boolean", "default": False},
            "refresh_structure": {"type": "boolean", "default": False},
        },
        "required": ["issue"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        provider: LLMProvider,
        bus: EventBus,
        run_id: str,
        workspace_root: Path | None,
    ) -> None:
        self._provider = provider
        self._bus = bus
        self._run_id = run_id
        self._workspace_root = workspace_root.resolve() if workspace_root is not None else None
        self._step = 0
        self._usage = _Usage()

    async def _chat(
        self,
        messages: list[dict[str, object]],
        *,
        system: str,
        tools: list[dict[str, object]] | None = None,
    ) -> LlmResponse:
        self._step += 1
        response = await self._provider.chat(
            messages=messages,
            tool_schemas=tools or [],
            bus=self._bus,
            run_id=self._run_id,
            step=self._step,
            system=system,
        )
        self._usage.add(response)
        return response

    async def _select_files(
        self,
        issue: str,
        structure_text: str,
        valid_files: set[str],
        top_k: int,
    ) -> list[str]:
        response = await self._chat(
            [
                {
                    "role": "user",
                    "content": f"Issue:\n{issue}\n\nRepository structure:\n{structure_text}",
                }
            ],
            system=(
                "You localize software issues. Select initially suspicious implementation files. "
                "Return only JSON {\"files\": [..]} with at most "
                f"{max(top_k * 2, 8)} exact paths."
            ),
        )
        return _file_list(response.text, valid_files, max(top_k * 2, 8))

    async def _reflect_files(
        self,
        issue: str,
        structure_text: str,
        candidates: list[str],
        top_k: int,
    ) -> list[str]:
        response = await self._chat(
            [
                {
                    "role": "user",
                    "content": (
                        f"Issue:\n{issue}\n\nRepository structure:\n{structure_text}\n\n"
                        "Initial files plus one-hop import/dependent files:\n"
                        f"{json.dumps(candidates)}"
                    ),
                }
            ],
            system=(
                "Rerank the candidate files using both the issue and static dependency context. "
                f"Return only JSON {{\"files\": [..]}} with the best {top_k} exact paths."
            ),
        )
        return _file_list(response.text, set(candidates), top_k)

    async def _prune(self, issue: str, label: str, source: str) -> tuple[bool, str]:
        response = await self._chat(
            [
                {
                    "role": "user",
                    "content": f"Issue:\n{issue}\n\nRetrieved symbol: {label}\n\nSource:\n{source}",
                }
            ],
            system=(
                "Judge whether this source is materially relevant to the issue. Return only JSON "
                "with boolean key keep and a short string reason. Be conservative with context."
            ),
        )
        try:
            parsed = _extract_json(response.text)
            if isinstance(parsed, dict):
                return parsed.get("keep") is True, str(parsed.get("reason", ""))
        except ValueError:
            pass
        return False, "pruner returned an invalid decision"

    async def _localize_symbols(
        self,
        issue: str,
        root: Path,
        structure: dict[str, dict[str, object]],
        files: list[str],
        max_rounds: int,
        top_k_symbols: int,
    ) -> tuple[dict[str, list[str]], list[dict[str, object]]]:
        outline = _symbol_outline(structure, files)
        messages: list[dict[str, object]] = [
            {
                "role": "user",
                "content": (
                    f"Issue:\n{issue}\n\nCandidate symbol structure:\n{outline}\n\n"
                    "Inspect only the most relevant classes/functions. Call cosil_exit when ready."
                ),
            }
        ]
        evidence: list[dict[str, object]] = []
        for _ in range(max_rounds):
            response = await self._chat(
                messages,
                system=(
                    "You are the function-level CoSIL search agent. Use the source retrieval tools "
                    "to investigate candidate symbols. Do not guess source that you have not read."
                ),
                tools=_LOCATION_TOOL_SCHEMAS,
            )
            assistant_blocks: list[dict[str, object]] = []
            if response.text:
                assistant_blocks.append({"type": "text", "text": response.text})
            assistant_blocks.extend(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}
                for call in response.tool_calls
            )
            messages.append({"role": "assistant", "content": assistant_blocks})
            if not response.tool_calls:
                break

            tool_results: list[dict[str, object]] = []
            called_exit = False
            for call in response.tool_calls:
                if call.name == "cosil_exit":
                    called_exit = True
                    content = "Source inspection complete."
                else:
                    try:
                        symbol, source = _find_symbol(root, structure, call)
                        file_name = str(call.input.get("file_name", ""))
                        keep, reason = await self._prune(
                            issue, f"{file_name}::{symbol}", source
                        )
                        evidence.append(
                            {
                                "file": file_name,
                                "symbol": symbol,
                                "kept": keep,
                                "reason": reason,
                                "source": source if keep else "",
                            }
                        )
                        content = source if keep else f"Pruned as unrelated: {reason}"
                    except (OSError, ValueError, PermissionError) as exc:
                        content = f"Tool call failed: {exc}"
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": call.id, "content": content}
                )
            messages.append({"role": "user", "content": tool_results})
            if called_exit:
                break

        kept = [item for item in evidence if item["kept"]]
        compact_evidence = [
            {"file": item["file"], "symbol": item["symbol"], "reason": item["reason"]}
            for item in kept
        ]
        summary = await self._chat(
            [
                {
                    "role": "user",
                    "content": (
                        f"Issue:\n{issue}\n\nCandidate symbols:\n{outline}\n\n"
                        f"Kept evidence:\n{json.dumps(compact_evidence, ensure_ascii=False)}"
                    ),
                }
            ],
            system=(
                "Produce the final function-level localization. Return only JSON with key "
                f"locations mapping exact file paths to arrays of symbol labels, with at most "
                f"{top_k_symbols} symbols total. Use labels like 'class: A.m' or 'function: f'."
            ),
        )
        locations: dict[str, list[str]] = {}
        try:
            parsed = _extract_json(summary.text)
            raw_locations = parsed.get("locations", {}) if isinstance(parsed, dict) else {}
            if isinstance(raw_locations, dict):
                count = 0
                for file_name, symbols in raw_locations.items():
                    if file_name not in files or not isinstance(symbols, list):
                        continue
                    clean = [str(symbol) for symbol in symbols if str(symbol).strip()]
                    if clean:
                        remaining = top_k_symbols - count
                        locations[str(file_name)] = clean[:remaining]
                        count += len(locations[str(file_name)])
                    if count >= top_k_symbols:
                        break
        except ValueError:
            pass
        if not locations:
            for item in kept[:top_k_symbols]:
                locations.setdefault(str(item["file"]), []).append(str(item["symbol"]))
        return locations, evidence

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = CosilLocalizeParams.model_validate(params)
        self._usage = _Usage()
        self._step = 0
        if self._workspace_root is None:
            return ToolResult(
                content="cosil_localize requires a session workspace",
                is_error=True,
                error_type="runtime_error",
            )
        root = self._workspace_root
        if not root.is_dir():
            return ToolResult(
                content=f"workspace does not exist: {root}",
                is_error=True,
                error_type="runtime_error",
            )

        try:
            instance_id, structure, cache_path = build_repository_structure(
                root,
                include_tests=parsed.include_tests,
                refresh=parsed.refresh_structure,
            )
            if not structure:
                raise ValueError("no Python files found in the workspace")
            structure_text = _render_structure(structure)
            valid_files = set(structure)
            initial = await self._select_files(
                parsed.issue, structure_text, valid_files, parsed.top_k_files
            )
            if not initial:
                raise ValueError("file-level model did not select a valid repository file")
            expanded = _dependency_candidates(structure, initial)
            found_files = await self._reflect_files(
                parsed.issue, structure_text, expanded, parsed.top_k_files
            )
            if not found_files:
                found_files = initial[: parsed.top_k_files]
            locations, evidence = await self._localize_symbols(
                parsed.issue,
                root,
                structure,
                found_files,
                parsed.max_rounds,
                parsed.top_k_symbols,
            )
        except Exception as exc:
            return ToolResult(
                content=f"cosil localization failed: {exc}",
                is_error=True,
                error_type="runtime_error",
            )

        output = {
            "instance_id": instance_id,
            "found_files": found_files,
            "found_related_locs": locations,
            "evidence": [
                {
                    "file": item["file"],
                    "symbol": item["symbol"],
                    "kept": item["kept"],
                    "reason": item["reason"],
                }
                for item in evidence
            ],
            "structure_cache": cache_path.relative_to(root).as_posix(),
            "usage": {
                "prompt_tokens": self._usage.input_tokens,
                "completion_tokens": self._usage.output_tokens,
            },
        }
        return ToolResult(content=json.dumps(output, ensure_ascii=False, indent=2))
