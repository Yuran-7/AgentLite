from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from agent_lite.core.permissions.classifier import AutoModeClassifier, Verdict
from agent_lite.core.permissions.policy import (
    DEFAULT_POLICIES,
    SHELL_TOOL_NAMES,
    PermissionDecision,
    ToolPolicy,
    matches_outside_cwd,
    param_preview,
)
from agent_lite.core.permissions.rules import (
    EDIT_TOOLS,
    QUERY_TOOLS,
    dangerous_shell,
    edit_target,
    protected_path,
    protected_shell,
    readonly_shell,
    shell_kind,
    within,
)
from agent_lite.core.permissions.storage import load_policy_file, save_policy_file
from agent_lite.core.permissions.types import APPROVAL_DECISIONS, AUTO_DECISIONS, PermissionMode

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


@dataclass
class PermissionContext:
    mode_getter: Callable[[], PermissionMode]
    workspace_root: Path | None = None
    classifier: AutoModeClassifier | None = None
    user_messages_getter: Callable[[], list[str]] = field(default=lambda: [])
    tool_calls_getter: Callable[[], list[dict[str, Any]]] = field(default=lambda: [])
    shell: str = field(default_factory=shell_kind)


@dataclass
class _PendingRequest:
    future: asyncio.Future[str]
    session_id: str
    tool_name: str
    allowed_decisions: tuple[str, ...] = APPROVAL_DECISIONS


# 管理工具调用权限：策略评估、用户审批挂起、session 级和持久化 always 缓存、超时
# CoreApp的成员变量
class PermissionManager:
    def __init__(
        self,
        policies: dict[str, ToolPolicy] | None = None,
        *,
        policy_file: Path | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._policies: dict[str, ToolPolicy] = policies or dict(DEFAULT_POLICIES)
        # tool_use_id → pending Future + metadata
        self._pending: dict[str, _PendingRequest] = {}
        # (session_id, tool_name) → "allow" | "deny"（session 内存，重启丢失）
        self._session_always: dict[tuple[str, str], str] = {}
        # tool_name → "allow" | "deny"（持久化，从 policy_file 加载）
        self._policy_file = policy_file
        self._persistent_always: dict[str, str] = (
            load_policy_file(policy_file) if policy_file is not None else {}
        )
        # 0 表示不超时
        self._timeout_s = timeout_s

    # 对工具名 + 参数执行 4 层静态评估，不挂起
    def evaluate(self, tool_name: str, params: dict[str, Any]) -> PermissionDecision:
        from agent_lite.core.permissions.policy import evaluate

        policy = self._policies.get(tool_name)
        if policy is None and tool_name == "shell":
            policy = self._policies.get("bash")
        return evaluate(tool_name, params, policy)

    # 检查权限；如需 ask 则向客户端发事件并等待响应；返回 (allowed, decision_str)
    async def check_and_wait(
        self,
        tool_use_id: str,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        event_emitter: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        context: PermissionContext | None = None,
    ) -> tuple[bool, str]:
        mode = context.mode_getter() if context is not None else "manual"
        if context is not None and mode != "manual":
            result = await self._check_mode(
                tool_use_id,
                tool_name,
                params,
                session_id,
                event_emitter,
                context,
                mode,
            )
            if result is not None:
                return result
        cache_tool_name = "shell" if tool_name == "bash" else tool_name
        command = str(params.get("command", "")) if tool_name in SHELL_TOOL_NAMES else ""
        policy = self._policies.get(tool_name) or self._policies.get(cache_tool_name)
        if policy is None and cache_tool_name == "shell":
            policy = self._policies.get("bash")

        # Tier 1: deny_patterns（shell only，不可被缓存绕过）
        if command and policy:
            for pat in policy.deny_patterns:
                if re.search(pat, command):
                    logger.debug("permission: deny_pattern hit tool=%s", tool_name)
                    return False, "auto_deny"

        # Tier 2: OUTSIDE_CWD_HEURISTICS（shell only，强制 ASK，不可被任何缓存绕过）
        outside_cwd = bool(command and matches_outside_cwd(command))

        if not outside_cwd:
            # Tier 3: session always 缓存
            session_key = (session_id, cache_tool_name)
            if session_key in self._session_always:
                cached = self._session_always[session_key]
                logger.debug("permission: session cache hit tool=%s decision=%s", tool_name, cached)
                return cached == "allow", f"auto_{cached}"

            # Tier 4: persistent always（跨 session）
            if cache_tool_name in self._persistent_always:
                cached = self._persistent_always[cache_tool_name]
                logger.debug(
                    "permission: persistent cache hit tool=%s decision=%s",
                    tool_name,
                    cached,
                )
                return cached == "allow", f"auto_{cached}"

            # Tier 5: allow_patterns（shell only）
            if command and policy:
                for pat in policy.allow_patterns:
                    if re.search(pat, command):
                        return True, "auto_allow"

            # Tier 6: tool default
            if policy is not None:
                if policy.default == PermissionDecision.ALLOW:
                    return True, "auto_allow"
                if policy.default == PermissionDecision.DENY:
                    return False, "auto_deny"
            # default == ASK（shell、unknown tool）→ fall through to Future

        return await self._ask(tool_use_id, tool_name, params, session_id, event_emitter)

    # 对新模式执行硬规则，编辑模式普通操作可回退旧策略。
    async def _check_mode(
        self,
        uid: str,
        name: str,
        params: dict[str, Any],
        sid: str,
        emitter: Callable[[dict[str, Any]], Awaitable[None]],
        context: PermissionContext,
        mode: PermissionMode,
    ) -> tuple[bool, str] | None:
        canonical = "shell" if name == "bash" else name
        policy = self._policies.get(name) or self._policies.get(canonical)
        if policy is None and canonical == "shell":
            policy = self._policies.get("bash")
        command = str(params.get("command", "")) if canonical == "shell" else ""
        if (
            self._session_always.get((sid, canonical)) == "deny"
            or self._persistent_always.get(canonical) == "deny"
            or (policy is not None and policy.default == PermissionDecision.DENY)
            or (
                command
                and policy is not None
                and any(re.search(pat, command) for pat in policy.deny_patterns)
            )
        ):
            return False, "auto_deny"
        code, reason = "", ""
        target = edit_target(params, context.workspace_root) if name in EDIT_TOOLS else None
        resolved_target: Path | None = None
        try:
            resolved_target = target.resolve() if target is not None else None
            if target is not None and protected_path(target, context.workspace_root):
                code, reason = "protected_path", "目标是受保护路径，需人工确认"
            elif command and dangerous_shell(command):
                code, reason = "dangerous_shell", "命令包含高危操作，需人工确认"
            elif command and protected_shell(command, context.workspace_root, context.shell):
                code, reason = "protected_path", "命令涉及受保护路径，需人工确认"
            elif name in QUERY_TOOLS or name == "spawn_agent":
                return True, "auto_allow"
            elif (
                target is not None
                and context.workspace_root is not None
                and within(target, context.workspace_root)
            ):
                return True, "auto_allow"
            elif command and readonly_shell(command, context.workspace_root, context.shell):
                return True, "auto_allow"
            elif mode == "accept_edits" and name in EDIT_TOOLS:
                code, reason = "outside_workspace", "写入目标不在已确认工作区内，需人工确认"
        except (OSError, ValueError, RuntimeError):
            code, reason = "path_unresolved", "无法可靠解析目标路径，需人工确认"
        if not code and mode == "accept_edits":
            return None
        if not code:
            if context.classifier is None:
                code, reason = "classifier_disabled", "自动分类器未启用，需人工确认"
            else:
                action = {
                    "tool_name": name,
                    "params": params,
                    "target_path": str(resolved_target) if resolved_target is not None else None,
                    "workspace_root": (
                        str(context.workspace_root) if context.workspace_root else None
                    ),
                    "platform": os.name,
                    "shell": context.shell,
                }
                try:
                    verdict = await context.classifier.classify(
                        action,
                        context.user_messages_getter(),
                        context.tool_calls_getter(),
                    )
                except Exception:
                    verdict = Verdict("block", "分类上下文不可用，需人工确认", "classifier_error")
                if verdict.decision == "allow":
                    return True, "classifier_allow"
                code, reason = verdict.reason_code, verdict.reason
        return await self._ask(
            uid, name, params, sid, emitter, mode=mode, reason_code=code, reason=reason
        )

    # 发布审批并等待响应，超时、取消或事件发布失败时均清理挂起状态。
    async def _ask(
        self,
        uid: str,
        name: str,
        params: dict[str, Any],
        sid: str,
        emitter: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        mode: PermissionMode | None = None,
        reason_code: str | None = None,
        reason: str | None = None,
    ) -> tuple[bool, str]:
        canonical = "shell" if name == "bash" else name
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        decisions = AUTO_DECISIONS if mode == "auto" else APPROVAL_DECISIONS
        self._pending[uid] = _PendingRequest(future, sid, canonical, decisions)
        event: dict[str, Any] = {
            "type": "permission.requested",
            "tool_use_id": uid,
            "tool_name": name,
            "params": params,
            "param_preview": param_preview(name, params),
            "session_id": sid,
            "ts": _now(),
        }
        if mode is not None:
            event.update(
                mode=mode, reason_code=reason_code, reason=reason, allowed_decisions=list(decisions)
            )
        try:
            await emitter(event)
            raw = (
                await asyncio.wait_for(future, self._timeout_s)
                if self._timeout_s > 0
                else await future
            )
            return self._apply_response(raw, sid, canonical), raw
        except TimeoutError:
            return False, "timeout"
        finally:
            self._pending.pop(uid, None)
            if not future.done():
                future.cancel()

    # 处理客户端返回的审批决策，resolve 对应 Future
    def respond(self, tool_use_id: str, decision: str) -> None:
        req = self._pending.get(tool_use_id)
        if req is None:
            logger.warning("permission.respond: unknown tool_use_id=%s", tool_use_id)
            return
        if decision not in req.allowed_decisions:
            raise ValueError("decision is not available for this approval")
        self._pending.pop(tool_use_id, None)
        if not req.future.done():
            req.future.set_result(decision)

    # 应用审批决策，更新 session + persistent 缓存，返回是否放行
    def _apply_response(self, decision: str, session_id: str, tool_name: str) -> bool:
        allow = decision in ("allow_once", "always_allow")
        if decision == "always_allow":
            self._session_always[(session_id, tool_name)] = "allow"
            self._persistent_always[tool_name] = "allow"
            logger.info(
                "permission: always allow tool=%s policy_file=%s persistent=%s",
                tool_name,
                self._policy_file,
                self._persistent_always,
            )
            if self._policy_file is not None:
                try:
                    save_policy_file(self._persistent_always, self._policy_file)
                    logger.info("permission: policy.toml written path=%s", self._policy_file)
                except Exception:
                    logger.exception(
                        "permission: failed to write policy.toml path=%s",
                        self._policy_file,
                    )
            else:
                logger.warning("permission: policy_file is None, skipping persistence")
        elif decision == "always_deny":
            self._session_always[(session_id, tool_name)] = "deny"
            self._persistent_always[tool_name] = "deny"
            logger.info(
                "permission: always deny tool=%s policy_file=%s persistent=%s",
                tool_name,
                self._policy_file,
                self._persistent_always,
            )
            if self._policy_file is not None:
                try:
                    save_policy_file(self._persistent_always, self._policy_file)
                    logger.info("permission: policy.toml written path=%s", self._policy_file)
                except Exception:
                    logger.exception(
                        "permission: failed to write policy.toml path=%s",
                        self._policy_file,
                    )
            else:
                logger.warning("permission: policy_file is None, skipping persistence")
        return allow

    # 客户端断连时拒绝该 session 所有待审批请求，防止 Future 永久挂起
    def cancel_session(self, session_id: str, reason: str = "client_disconnected") -> None:
        to_cancel = [uid for uid, req in self._pending.items() if req.session_id == session_id]
        for uid in to_cancel:
            req = self._pending.pop(uid)
            if not req.future.done():
                logger.debug("permission: cancel pending tool_use_id=%s reason=%s", uid, reason)
                req.future.set_result("deny_once")
