import { createServer, type Socket, type Server } from 'node:net';
import { RpcClient } from '../src/rpc';

// 建立真实 TCP 测试服务并记录所有连接，确保测试结束时清理。
export async function server(handler: (socket: Socket, message: Record<string, any>) => void): Promise<{
  port: number; listener: Server; close: () => Promise<void>
}> {
  const sockets = new Set<Socket>();
  const listener = createServer(socket => {
    sockets.add(socket); socket.on('close', () => sockets.delete(socket));
    let buffer = '';
    socket.on('data', chunk => {
      buffer += chunk.toString();
      while (buffer.includes('\n')) {
        const index = buffer.indexOf('\n'); const line = buffer.slice(0, index); buffer = buffer.slice(index + 1);
        handler(socket, JSON.parse(line));
      }
    });
  });
  await new Promise<void>(resolve => listener.listen(0, '127.0.0.1', resolve));
  return { port: (listener.address() as { port: number }).port, listener,
    close: async () => {
      for (const socket of sockets) socket.destroy();
      await new Promise<void>(resolve => listener.close(() => resolve()));
    } };
}

// 编码与生产 core 一致的 RPC 成功响应。
export function reply(socket: Socket, id: string, result: unknown): void {
  socket.write(JSON.stringify({ jsonrpc: '2.0', id, result }) + '\n');
}

// 等待可观察条件，超时时给出明确诊断。
export async function waitFor(condition: () => boolean | Promise<boolean>, timeout = 5000): Promise<void> {
  const deadline = Date.now() + timeout;
  while (!await condition()) {
    if (Date.now() >= deadline) throw new Error('等待测试条件超时');
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

// 获取未监听的随机端口供启动路径测试使用。
export async function freePort(): Promise<number> {
  const fixture = await server(() => {}); const port = fixture.port; await fixture.close(); return port;
}
