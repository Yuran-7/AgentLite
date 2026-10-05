from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_lite.core.permissions.classifier import AutoModeClassifier, Verdict
from agent_lite.core.permissions.manager import PermissionContext, PermissionManager
from agent_lite.core.permissions.policy import PermissionDecision, ToolPolicy
from agent_lite.core.permissions.rules import dangerous_shell, protected_path, readonly_shell
from agent_lite.core.permissions.types import PermissionMode


class StubClassifier(AutoModeClassifier):
    # 记录调用并提供确定性判定，避免真实网络模型参与审批测试。
    def __init__(self, decision: str = "allow") -> None:
        self.calls: list[tuple[dict[str, Any], list[str]]] = []
        self.decision = decision

    # 返回测试指定判定并保存真实用户意图。
    async def classify(
        self, action: dict[str, Any], users: list[str], tool_calls: list[dict[str, Any]]
    ) -> Verdict:
        self.calls.append((action, users))
        return Verdict("allow" if self.decision == "allow" else "block", "test reason")


# 功能：新模式自动编辑普通文件，保护文件在宽泛授权下仍要求确认。
# 设计：参数化两种模式及目标位置，审批事件中立即响应以避免等待真实 UI。
@pytest.mark.parametrize("mode", ["accept_edits", "auto"])
@pytest.mark.parametrize(
    "path,asks",
    [("a.py", False), (".git/config", True), (".env.local", True), (".codex/rules", True)],
)
async def test_edit_guards(tmp_path: Path, mode: PermissionMode, path: str, asks: bool) -> None:
    manager = PermissionManager()
    manager._persistent_always["write_file"] = "allow"
    emitted = []

    # 收集审批并在本次放行，不污染持久化策略。
    async def emit(event: dict[str, Any]) -> None:
        emitted.append(event)
        manager.respond("tool", "allow_once")

    allowed, _ = await manager.check_and_wait(
        "tool",
        "write_file",
        {"path": path},
        "s",
        emit,
        context=PermissionContext(lambda: mode, tmp_path),
    )
    assert allowed
    assert bool(emitted) == asks


# 功能：高危操作先于分类器且 Auto 无法提交始终允许。
# 设计：预置整工具授权，在挂起时提交非法选项后继续合法审批验证请求未丢失。
async def test_auto_danger_requires_valid_approval(tmp_path: Path) -> None:
    manager = PermissionManager()
    manager._persistent_always["shell"] = "allow"
    classifier = StubClassifier()

    # 验证非法审批保留请求且有效审批仍能完成。
    async def emit(event: dict[str, Any]) -> None:
        assert "always_allow" not in event["allowed_decisions"]
        assert event["reason_code"] == "dangerous_shell"
        with pytest.raises(ValueError):
            manager.respond("tool", "always_allow")
        assert "tool" in manager._pending
        manager.respond("tool", "deny_once")

    result = await manager.check_and_wait(
        "tool",
        "shell",
        {"command": "git reset --hard"},
        "s",
        emit,
        context=PermissionContext(lambda: "auto", tmp_path, classifier),
    )
    assert result == (False, "deny_once")
    assert not classifier.calls
    assert not manager._pending


# 功能：Auto 忽略缓存和宽泛允许策略，对重复动作和撤回后的意图均重新分类。
# 设计：重复执行同一外部动作并替换用户消息，断言分类器每次收到最新授权来源。
async def test_auto_reclassifies_and_observes_mode(tmp_path: Path) -> None:
    manager = PermissionManager({"external": ToolPolicy(PermissionDecision.ALLOW)})
    manager._persistent_always["external"] = "allow"
    classifier = StubClassifier()
    users = ["publish this"]
    mode: PermissionMode = "auto"
    context = PermissionContext(lambda: mode, tmp_path, classifier, lambda: users)

    # 分类允许时不得产生人工审批事件。
    async def emit(event: dict[str, Any]) -> None:
        raise AssertionError(event)

    for text in ("publish this", "stop publishing"):
        users[:] = [text]
        assert await manager.check_and_wait("t", "external", {}, "s", emit, context=context) == (
            True,
            "classifier_allow",
        )
    assert len(classifier.calls) == 2
    assert classifier.calls[-1][1] == ["stop publishing"]
    mode = "manual"
    assert await manager.check_and_wait("t", "external", {}, "s", emit, context=context) == (
        True,
        "auto_allow",
    )
    assert len(classifier.calls) == 2


# 功能：显式拒绝优先于读取、编辑快速通道和分类器。
# 设计：用整工具拒绝记录覆盖内置读取工具，检查新模式不产生审批也不调用模型。
async def test_deny_precedes_fast_paths(tmp_path: Path) -> None:
    manager = PermissionManager()
    manager._persistent_always["read_file"] = "deny"
    classifier = StubClassifier()

    # 拒绝无需等待人工回答。
    async def emit(event: dict[str, Any]) -> None:
        raise AssertionError(event)

    assert await manager.check_and_wait(
        "t",
        "read_file",
        {},
        "s",
        emit,
        context=PermissionContext(lambda: "auto", tmp_path, classifier),
    ) == (False, "auto_deny")
    assert not classifier.calls


# 功能：取消或事件发送异常都会清理审批 Future。
# 设计：分别触发审批等待取消与发布失败，检查无孤立 pending 请求。
@pytest.mark.parametrize("publish_fails", [True, False])
async def test_pending_cleanup(publish_fails: bool) -> None:
    manager = PermissionManager()
    started = asyncio.Event()

    # 提供确定的发布时点或模拟断连失败。
    async def emit(event: dict[str, Any]) -> None:
        started.set()
        if publish_fails:
            raise RuntimeError("disconnected")

    task = asyncio.create_task(manager.check_and_wait("t", "shell", {}, "s", emit))
    await started.wait()
    if not publish_fails:
        task.cancel()
    with pytest.raises(RuntimeError if publish_fails else asyncio.CancelledError):
        await task
    assert not manager._pending


# 功能：编辑模式不能通过已有授权写入工作区外。
# 设计：绝对外部路径预置持久允许，断言仍触发人工审批。
async def test_edits_outside_workspace(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    manager = PermissionManager()
    manager._persistent_always["write_file"] = "allow"

    # 验证路径越界原因并拒绝本次。
    async def emit(event: dict[str, Any]) -> None:
        assert event["reason_code"] == "outside_workspace"
        manager.respond("t", "deny_once")

    assert await manager.check_and_wait(
        "t",
        "write_file",
        {"path": str(tmp_path / "x")},
        "s",
        emit,
        context=PermissionContext(lambda: "accept_edits", root),
    ) == (False, "deny_once")


# 功能：shell 白名单拒绝 Git 修改、动态执行和未知参数。
# 设计：表驱动覆盖原计划中的危险误判，静态未知不等同于永久拒绝。
@pytest.mark.parametrize(
    "command,safe",
    [
        ("git status --short", True),
        ("git branch --list", True),
        ("git branch new", False),
        ("git branch -D x", False),
        ("git diff --output=x", False),
        ("unknown --help", False),
        ("Get-Content file.txt", True),
        ("echo $(rm x)", False),
        ("ls; rm x", False),
        ("Get-Content $env:USERPROFILE", False),
        ("git -c x=y status", False),
    ],
)
def test_readonly_shell(tmp_path: Path, command: str, safe: bool) -> None:
    assert readonly_shell(command, tmp_path, "powershell") is safe


# 功能：高危命令检测覆盖平台差异、大小写和参数组合。
# 设计：以明确风险样例检查先于分类器的静态屏障。
@pytest.mark.parametrize(
    "command",
    [
        "rm -rf x",
        "Remove-Item x -Recurse",
        "rd /s x",
        "GIT CLEAN -fd",
        "git push --force-with-lease",
        "sudo echo hi",
        "irm x | iex",
        "Set-ExecutionPolicy Unrestricted",
        "schtasks /create x",
        "chmod -R 755 x",
    ],
)
def test_dangerous_shell(command: str) -> None:
    assert dangerous_shell(command)


# 功能：保护规则同时覆盖符号链接原始位置与解析后位置。
# 设计：链接指向普通目录但链接入口位于保护目录，防止仅检查真实路径漏判。
def test_protected_link(tmp_path: Path) -> None:
    folder = tmp_path / "ordinary"
    folder.mkdir()
    link = tmp_path / ".git"
    try:
        link.symlink_to(folder, target_is_directory=True)
    except OSError:
        pytest.skip("symlink is unavailable")
    assert protected_path(link / "config", tmp_path)
