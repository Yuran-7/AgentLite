from __future__ import annotations

import asyncio
import json
import sys
import time

import agent_lite
from agent_lite.core.bus.commands import PongResult
from agent_lite.core.bus.envelope import JsonRpcError, JsonRpcSuccess
from agent_lite.core.config import AgentLiteConfig


# 同步入口：运行 _ping 协程，连接失败时打印错误并退出
def cmd_ping(config: AgentLiteConfig) -> None:
    try:
        asyncio.run(_ping(config))  # 创建并管理事件循环，直到 _ping 执行完毕
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        sys.exit(1)


# 向 core 守护进程发送 ping 请求，打印 pong 响应及延迟
# 当前 _ping 协程有 4 个显式 await，网络 I/O 期间会将控制权交还事件循环
async def _ping(config: AgentLiteConfig) -> None:
    t0 = time.monotonic()
    reader, writer = await asyncio.open_connection(config.host, config.port)  # 以异步（非阻塞）方式建立一个 TCP 客户端连接

    req = {
        "jsonrpc": "2.0", # JSON-RPC 2.0 协议版本
        "id": "cli-1",
        "method": "core.ping",  # 要调用的远程方法
        "params": {"client": f"cli/{agent_lite.__version__}"},
    }
    # json.dumps(req) 会将字典转换为 JSON 字符串，末尾加上换行符 \n 以便服务端按行读取
    # NDJSON（Newline Delimited JSON）是一种常用的流式 JSON 格式，每行都是一个独立的 JSON 对象，适合网络传输和日志记录
    # encode() 的作用是将字符串（str）编码为字节串（bytes）对象
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()

    line = await asyncio.wait_for(reader.readline(), timeout=10.0)
    latency_ms = int((time.monotonic() - t0) * 1000)

    writer.close()
    await writer.wait_closed()

    raw = json.loads(line)
    if "error" in raw:
        err = JsonRpcError.model_validate(raw)
        print(f"error: {err.error.code} {err.error.message}", file=sys.stderr)
        sys.exit(1)

    resp = JsonRpcSuccess.model_validate(raw)
    result = PongResult.model_validate(resp.result)
    print(f"pong server={result.server_version} uptime={result.uptime_ms}ms latency={latency_ms}ms")
