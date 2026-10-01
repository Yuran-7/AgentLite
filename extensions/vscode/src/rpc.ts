import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import { Socket } from 'node:net';
import type { CoreEvent } from './protocol';

export const MAX_FRAME_BYTES = 64 * 1024 * 1024;
export class RpcError extends Error {
  // 保留 core 的错误码供调用者区分业务错误。
  constructor(public readonly code: number, message: string) { super(message); }
}
type Pending = { resolve: (value: Record<string, unknown>) => void;
  reject: (error: Error) => void; timer?: NodeJS.Timeout };

export class RpcClient extends EventEmitter {
  private chunks: Buffer[] = [];
  private bytes = 0;
  private pending = new Map<string, Pending>();
  private ended = false;
  private intentional = false;

  // 订阅原始 socket，按完整字节帧解码 UTF-8。
  constructor(private readonly socket: Socket) {
    super();
    socket.on('data', chunk => this.consume(chunk));
    socket.on('error', error => this.fail(error));
    socket.on('close', () => this.fail(new Error('core 连接已断开')));
  }

  // 建立带超时的本地 TCP 连接。
  static connect(port: number, timeoutMs = 1500): Promise<RpcClient> {
    return new Promise((resolve, reject) => {
      const socket = new Socket();
      const timer = setTimeout(() => socket.destroy(new Error('连接 core 超时')), timeoutMs);
      const failed = (error: Error) => { clearTimeout(timer); socket.destroy(); reject(error); };
      socket.once('error', failed);
      socket.connect(port, '127.0.0.1', () => {
        clearTimeout(timer); socket.off('error', failed); socket.setNoDelay(true);
        resolve(new RpcClient(socket));
      });
    });
  }

  // 发送 RPC；长任务使用 timeoutMs=0，由事件和断线管理其生命周期。
  request(method: string, params: Record<string, unknown>, timeoutMs = 10_000): Promise<Record<string, unknown>> {
    if (this.ended) return Promise.reject(new Error('core 未连接'));
    const id = randomUUID();
    const frame = Buffer.from(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    if (frame.length > MAX_FRAME_BYTES) return Promise.reject(new Error('请求超过 64 MB'));
    return new Promise((resolve, reject) => {
      const pending: Pending = { resolve, reject };
      if (timeoutMs > 0) pending.timer = setTimeout(() => {
        this.pending.delete(id); reject(new Error(`${method} 响应超时`));
      }, timeoutMs);
      this.pending.set(id, pending);
      this.socket.write(frame, error => { if (error) this.fail(error); });
    });
  }

  // 主动关闭连接，不向上层报告意外断线。
  close(): void {
    this.intentional = true;
    this.fail(new Error('连接已关闭'));
  }

  // 分割换行帧并限制累计大小，避免对每个 chunk 重复拷贝大结果。
  private consume(chunk: Buffer): void {
    let offset = 0;
    while (offset < chunk.length && !this.ended) {
      const newline = chunk.indexOf(10, offset);
      const end = newline < 0 ? chunk.length : newline;
      const part = chunk.subarray(offset, end);
      this.bytes += part.length;
      if (this.bytes > MAX_FRAME_BYTES) { this.fail(new Error('core 消息超过 64 MB')); return; }
      this.chunks.push(part);
      if (newline < 0) return;
      const line = Buffer.concat(this.chunks, this.bytes).toString('utf8');
      this.chunks = []; this.bytes = 0;
      if (line.trim()) this.dispatch(line);
      offset = newline + 1;
    }
  }

  // 将响应匹配到请求，将 event.push 转换为业务事件。
  private dispatch(line: string): void {
    try {
      const message = JSON.parse(line);
      if (!message || message.jsonrpc !== '2.0') throw new Error('无效 JSON-RPC 消息');
      if (message.method === 'event.push' && !('id' in message)) {
        if (!message.params || typeof message.params.type !== 'string') throw new Error('无效事件');
        this.emit('event', message.params as CoreEvent);
        return;
      }
      if (typeof message.id !== 'string' || ('result' in message) === ('error' in message)) {
        throw new Error('无效 RPC 响应');
      }
      const pending = this.pending.get(message.id);
      if (!pending) return;
      clearTimeout(pending.timer); this.pending.delete(message.id);
      if (message.error) pending.reject(new RpcError(message.error.code, String(message.error.message)));
      else pending.resolve(message.result ?? {});
    } catch (error) { this.fail(error instanceof Error ? error : new Error(String(error))); }
  }

  // 清理全部等待者并只发送一次断线通知。
  private fail(error: Error): void {
    if (this.ended) return;
    this.ended = true;
    this.socket.destroy();
    this.chunks = []; this.bytes = 0;
    for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(error); }
    this.pending.clear();
    if (!this.intentional) this.emit('disconnect', error);
  }
}
