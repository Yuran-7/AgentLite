from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

import agent_lite.core.app  # noqa: F401 初始化应用入口，避免现有包级循环导入
from agent_lite.core.bus.envelope import HandlerError
from agent_lite.core.lifecycle import FrontendLifecycle


# 为租约测试创建未关闭的连接，观察过期时是否关闭连接
def connection() -> Mock:
    return Mock(is_closing=Mock(return_value=False))


# 功能：两个前端共用 core，最后一个退出才停止，宽限期重连会取消退出
# 设计：缩短计时器验证真实异步回调，不依赖任务管理器或外部模型
@pytest.mark.asyncio
async def test_last_frontend_and_reconnect() -> None:
    shutdown = asyncio.Event()
    lifecycle = FrontendLifecycle(shutdown.set, managed=True, idle_s=0.05, lease_s=1)
    a, b = connection(), connection()
    lifecycle.start()
    lifecycle.register(a)
    lifecycle.register(b)
    lifecycle.unregister(a)
    await asyncio.sleep(0.08)
    assert not shutdown.is_set()
    lifecycle.unregister(b)
    lifecycle.register(a)
    await asyncio.sleep(0.08)
    assert not shutdown.is_set()
    lifecycle.unregister(a)
    await asyncio.wait_for(shutdown.wait(), timeout=0.5)
    with pytest.raises(HandlerError):
        lifecycle.register(b)


# 功能：失效前端心跳到期会关闭连接，而正常续约可保持存活
# 设计：直接观察租约到期与关闭回调，覆盖挂起前端和续约后的旧计时器取消
@pytest.mark.asyncio
async def test_heartbeat_and_expiry() -> None:
    shutdown = asyncio.Event()
    lifecycle = FrontendLifecycle(shutdown.set, managed=True, idle_s=0.03, lease_s=0.15)
    writer = connection()
    lifecycle.register(writer, "tui")
    assert lifecycle.connection_counts() == {"vscode": 0, "tui": 1}
    await asyncio.sleep(0.1)
    lifecycle.heartbeat(writer)
    await asyncio.sleep(0.1)
    writer.close.assert_not_called()
    await asyncio.wait_for(shutdown.wait(), timeout=0.5)
    writer.close.assert_called_once()
    assert lifecycle.connection_counts() == {"vscode": 0, "tui": 0}


# 功能：手动常驻模式不会因前端退出停止，查询连接不能冒充前端续约
# 设计：比较空闲自动模式和常驻模式的实际计时器，并验证未登记的心跳被拒绝
@pytest.mark.asyncio
async def test_persistent_mode_and_unregistered_client() -> None:
    shutdown = asyncio.Event()
    lifecycle = FrontendLifecycle(shutdown.set, managed=False, idle_s=0.01, lease_s=1)
    writer = connection()
    lifecycle.start()
    with pytest.raises(HandlerError):
        lifecycle.heartbeat(writer)
    lifecycle.register(writer)
    lifecycle.unregister(writer)
    lifecycle.unregister(writer)
    await asyncio.sleep(0.05)
    assert not shutdown.is_set()
    lifecycle.close()


# 功能：手动启动能够将已运行的自动 core 转为常驻且取消待执行退出
# 设计：在最后一个前端离开的宽限期内提升模式，确认空闲计时器不再触发
@pytest.mark.asyncio
async def test_keep_alive_promotes_existing_core() -> None:
    shutdown = asyncio.Event()
    lifecycle = FrontendLifecycle(shutdown.set, managed=True, idle_s=0.03)
    writer = connection()
    lifecycle.register(writer)
    lifecycle.unregister(writer)
    lifecycle.keep_alive()
    await asyncio.sleep(0.06)
    assert not lifecycle.managed
    assert not shutdown.is_set()
    lifecycle.close()


# 功能：自动启动后无人登记会自动回收，断开的连接不能延长启动宽限期
# 设计：模拟启动方崩溃和晚到的登记请求，防止遗留空闲 core
@pytest.mark.asyncio
async def test_orphan_startup_and_closed_registration() -> None:
    shutdown = asyncio.Event()
    lifecycle = FrontendLifecycle(shutdown.set, managed=True, startup_s=0.03)
    writer = connection()
    writer.is_closing.return_value = True
    lifecycle.start()
    with pytest.raises(HandlerError):
        lifecycle.register(writer)
    await asyncio.wait_for(shutdown.wait(), timeout=0.5)


# 功能：连接统计区分前端类型，续约不重复计数，注销和关闭会移除连接
# 设计：交错登记三条连接并重登记同一连接，覆盖类型保留、类型切换和重复注销
@pytest.mark.asyncio
async def test_connection_counts_follow_active_leases() -> None:
    lifecycle = FrontendLifecycle(lambda: None, managed=False)
    a, b, c = connection(), connection(), connection()
    try:
        lifecycle.register(a, "vscode")
        lifecycle.register(b, "vscode")
        lifecycle.register(c, "tui")
        lifecycle.heartbeat(a)
        lifecycle.heartbeat(c)
        lifecycle.register(b, "vscode")
        assert lifecycle.connection_counts() == {"vscode": 2, "tui": 1}
        lifecycle.register(b, "tui")
        assert lifecycle.connection_counts() == {"vscode": 1, "tui": 2}
        lifecycle.unregister(c)
        lifecycle.unregister(c)
        assert lifecycle.connection_counts() == {"vscode": 1, "tui": 1}
    finally:
        lifecycle.close()
    assert lifecycle.connection_counts() == {"vscode": 0, "tui": 0}
