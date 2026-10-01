from __future__ import annotations

import asyncio
from collections.abc import Callable

from agent_lite.core.bus.envelope import INVALID_REQUEST, HandlerError


class FrontendLifecycle:
    # 按连接管理前端租约，自动模式在无人使用后触发正常退出
    def __init__(
        self, shutdown: Callable[[], None], *, managed: bool,
        idle_s: float = 15.0, lease_s: float = 45.0, startup_s: float = 60.0,
    ) -> None:
        self.managed = managed
        self.idle_s = idle_s
        self.lease_s = lease_s
        self.startup_s = startup_s
        self._shutdown = shutdown
        self._leases: dict[asyncio.StreamWriter, asyncio.TimerHandle] = {}
        self._idle: asyncio.TimerHandle | None = None
        self._stopping = False

    # 监听就绪后给首个前端注册留出时间，避免启动失败留下孤儿进程
    def start(self) -> None:
        if self.managed:
            self._idle = asyncio.get_running_loop().call_later(self.startup_s, self._expire_idle)

    # 注册连接或续约，拒绝已经断开或正在退出的连接
    def register(self, writer: asyncio.StreamWriter) -> None:
        if self._stopping or writer.is_closing():
            raise HandlerError(INVALID_REQUEST, "core is stopping or connection is closed")
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        previous = self._leases.pop(writer, None)
        if previous is not None:
            previous.cancel()
        self._leases[writer] = asyncio.get_running_loop().call_later(
            self.lease_s, self._expire_lease, writer
        )

    # 心跳仅允许已经登记的前端，普通查询连接不会延长 core 生命周期
    def heartbeat(self, writer: asyncio.StreamWriter) -> None:
        if writer not in self._leases:
            raise HandlerError(INVALID_REQUEST, "frontend must register first")
        self.register(writer)

    # 手动启动命令将已有自动 core 转为常驻，取消待执行的空闲退出
    def keep_alive(self) -> None:
        if self._stopping:
            raise HandlerError(INVALID_REQUEST, "core is stopping")
        self.managed = False
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    # 显式注销或 TCP 断线后释放租约，最后一个前端离开时开始宽限期
    def unregister(self, writer: asyncio.StreamWriter) -> None:
        lease = self._leases.pop(writer, None)
        if lease is None:
            return
        lease.cancel()
        if self.managed and not self._leases and not self._stopping:
            self._idle = asyncio.get_running_loop().call_later(self.idle_s, self._expire_idle)

    # 心跳超时后关闭失效连接，防止挂起前端继续发送任务
    def _expire_lease(self, writer: asyncio.StreamWriter) -> None:
        self.unregister(writer)
        writer.close()

    # 宽限期结束后锁定登记入口，再通知应用执行统一退出流程
    def _expire_idle(self) -> None:
        if not self._leases:
            self.close()
            self._shutdown()

    # 停止所有计时器，避免 core 清理期间再次触发生命周期回调
    def close(self) -> None:
        self._stopping = True
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        for lease in self._leases.values():
            lease.cancel()
        self._leases.clear()
