from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from agent_lite.core.bus.events import (
    PermissionDeniedEvent,
    PermissionGrantedEvent,
    PermissionRequestedEvent,
    ToolCallFailedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from agent_lite.core.events.bus import EventBus
from agent_lite.core.llm.types import ToolCallBlock
from agent_lite.core.tools.base import ToolResult
from agent_lite.core.tools.errors import RateLimitedError
from agent_lite.core.tools.file_operations import FileOperationError
from agent_lite.core.tools.registry import ToolRegistry
from agent_lite.core.tools.result_storage import ToolResultStore

if TYPE_CHECKING:
    from agent_lite.core.permissions.manager import PermissionManager

_DEFAULT_TIMEOUT: float = 120.0
_MAX_RETRIES: int = 2
_RETRY_BASE_S: float = 2.0  # backoff base; tests can monkeypatch to 0
_RETRYABLE: frozenset[str] = frozenset({"runtime_error", "rate_limited"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 发布 ToolCallFailedEvent 并返回对应 ToolResult
async def _fail(
    bus: EventBus,
    run_id: str,
    tool_call: ToolCallBlock,
    error_class: str,
    error_message: str,
    elapsed_ms: int,
    *,
    attempt: int = 1,
    result_store: ToolResultStore | None = None,
    token_budget: int | None = None,
) -> ToolResult:
    stored = (result_store or ToolResultStore(None)).prepare(
        error_message, run_id, f"{tool_call.id}-error-{attempt}", token_budget=token_budget,
    )
    await bus.publish(
        ToolCallFailedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            error_class=error_class,
            error_message=stored.content,
            output_path=stored.output_path,
            original_chars=stored.original_chars,
            truncated=stored.truncated,
            elapsed_ms=elapsed_ms,
            attempt=attempt,
            ts=_now(),
        )
    )
    return ToolResult(content=stored.content, is_error=True, error_type=error_class)


# 校验参数、检查权限、限时调用工具、发布进度事件，失败时指数退避重试，返回 ToolResult（不抛异常）
async def invoke_tool(
    registry: ToolRegistry,
    tool_call: ToolCallBlock,
    bus: EventBus,
    run_id: str,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    permission_manager: PermissionManager | None = None,
    session_id: str = "",
    result_store: ToolResultStore | None = None,
    token_budget: int | None = None,
) -> ToolResult:
    t0 = time.monotonic()
    result_store = result_store or ToolResultStore(None)

    await bus.publish(
        ToolCallStartedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            ts=_now(),
        )
    )

    def elapsed() -> int:
        return int((time.monotonic() - t0) * 1000)

    tool = registry.get(tool_call.name)
    if tool is None:
        return await _fail(
            bus, run_id, tool_call,
            "runtime_error", f"unknown tool: {tool_call.name}", elapsed(),
            result_store=result_store, token_budget=token_budget,
        )

    if tool.params_model is not None:
        try:
            tool.params_model.model_validate(dict(tool_call.input))
        except ValidationError as exc:
            return await _fail(
                bus, run_id, tool_call,
                "schema_error", str(exc), elapsed(),
                result_store=result_store, token_budget=token_budget,
            )

    if permission_manager is not None:
        async def _emit_permission(raw: dict[str, Any]) -> None:
            await bus.publish(PermissionRequestedEvent(**raw, run_id=run_id))

        allowed, decision = await permission_manager.check_and_wait(  # 核心函数
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            session_id=session_id,
            event_emitter=_emit_permission,
        )
        if allowed:
            if decision not in ("auto_allow",):
                await bus.publish(
                    PermissionGrantedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        decision=decision,
                        ts=_now(),
                    )
                )
        else:
            if decision != "auto_deny":
                await bus.publish(
                    PermissionDeniedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        decision=decision,
                        ts=_now(),
                    )
                )
            return await _fail(
                bus, run_id, tool_call,
                "permission_denied",
                "Permission denied by user. You may not execute this command. "
                "Try an alternative approach or ask the user what to do.",
                elapsed(),
                result_store=result_store, token_budget=token_budget,
            )

    for attempt in range(1, _MAX_RETRIES + 2):  # 进入这个for循环是因为权限检查通过了
        error_class: str | None = None
        error_message: str | None = None

        try:
            tool.set_call_context(run_id, tool_call.id)
            result = await asyncio.wait_for(
                tool.invoke(dict(tool_call.input)), timeout=timeout
            )
            ms = elapsed()

            if result.is_error:
                error_class = result.error_type or "runtime_error"
                error_message = result.content
            else:
                stored = await asyncio.to_thread(
                    result_store.prepare, result.content, run_id, tool_call.id,
                    token_budget=token_budget,
                )
                await bus.publish(
                    ToolCallFinishedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        tool_name=tool_call.name,
                        elapsed_ms=ms,
                        output=stored.content,
                        output_path=stored.output_path,
                        original_chars=stored.original_chars,
                        truncated=stored.truncated,
                        ts=_now(),
                    )
                )
                result.content = stored.content
                result.truncated = result.truncated or stored.truncated
                result.delivery_truncated = stored.truncated
                result.output_path = stored.output_path
                return result

        except FileOperationError as exc:
            error_class = exc.error_type
            error_message = str(exc)
        except RateLimitedError as exc:
            error_class = "rate_limited"
            error_message = str(exc)
        except TimeoutError:
            return await _fail(
                bus, run_id, tool_call,
                "timeout", f"tool timed out after {timeout}s", elapsed(),
                attempt=attempt,
                result_store=result_store, token_budget=token_budget,
            )
        except (OSError, UnicodeDecodeError) as exc:
            error_class = (
                "file_io" if tool_call.name in {"write_file", "edit_file"} else "runtime_error"
            )
            error_message = str(exc)
        except Exception as exc:
            error_class = "runtime_error"
            error_message = str(exc)

        assert error_class is not None and error_message is not None
        ms = elapsed()

        if error_class in _RETRYABLE and attempt <= _MAX_RETRIES:
            stored = result_store.prepare(
                error_message, run_id, f"{tool_call.id}-error-{attempt}",
                token_budget=token_budget,
            )
            await bus.publish(
                ToolCallFailedEvent(
                    run_id=run_id,
                    tool_use_id=tool_call.id,
                    tool_name=tool_call.name,
                    error_class=error_class,
                    error_message=stored.content,
                    output_path=stored.output_path,
                    original_chars=stored.original_chars,
                    truncated=stored.truncated,
                    elapsed_ms=ms,
                    attempt=attempt,
                    ts=_now(),
                )
            )
            await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
            continue

        return await _fail(
            bus, run_id, tool_call,
            error_class, error_message, ms,
            attempt=attempt,
            result_store=result_store, token_budget=token_budget,
        )

    # unreachable, but keeps mypy happy
    return ToolResult(content="internal error", is_error=True, error_type="runtime_error")
