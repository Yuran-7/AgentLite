import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RpcClient, RpcError, MAX_FRAME_BYTES } from '../src/rpc';
import { server, reply } from './helpers';

// 功能：验证中文跨字节拆包、粘包和响应与事件交错。
// 设计：真实 socket 在中文首字节后拆帧，同时先返回后发请求以排除顺序匹配错误。
test('TCP UTF-8 fragmentation, coalescing and request IDs', async () => {
  const requests: any[] = [];
  const fixture = await server((socket, message) => {
    requests.push(message);
    if (requests.length === 2) {
      const frames = Buffer.from(JSON.stringify({ jsonrpc: '2.0', method: 'event.push', params: { type: 'llm.token', token: '你好' } }) + '\n' +
        JSON.stringify({ jsonrpc: '2.0', id: requests[1].id, result: { value: 2 } }) + '\n' +
        JSON.stringify({ jsonrpc: '2.0', id: requests[0].id, result: { value: 1 } }) + '\n');
      const split = frames.indexOf(Buffer.from('你好')) + 1;
      socket.write(frames.subarray(0, split)); setTimeout(() => socket.write(frames.subarray(split)), 10);
    }
  });
  const client = await RpcClient.connect(fixture.port); const events: any[] = [];
  client.on('event', event => events.push(event));
  try {
    const [first, second] = await Promise.all([client.request('first', {}), client.request('second', {})]);
    assert.equal(first.value, 1); assert.equal(second.value, 2); assert.equal(events[0].token, '你好');
  } finally { client.close(); await fixture.close(); }
});

// 功能：验证业务错误、超时和断线分别结束等待请求。
// 设计：同一连接先返回错误，再模拟无响应和服务退出，检查客户端仍能复用与清理。
test('RPC errors, timeout, and disconnect cleanup', async () => {
  const fixture = await server((socket, message) => {
    if (message.method === 'error') socket.write(JSON.stringify({ jsonrpc: '2.0', id: message.id, error: { code: -32012, message: 'busy' } }) + '\n');
    if (message.method === 'ping') reply(socket, message.id, { ok: true });
    if (message.method === 'disconnect') socket.destroy();
  });
  const client = await RpcClient.connect(fixture.port);
  try {
    await assert.rejects(client.request('error', {}), (error: unknown) => error instanceof RpcError && error.code === -32012);
    await assert.rejects(client.request('timeout', {}, 20), /响应超时/);
    assert.equal((await client.request('ping', {})).ok, true);
    const pending = client.request('forever', {}, 0);
    await Promise.all([assert.rejects(pending, /断开/), assert.rejects(client.request('disconnect', {}), /断开/)]);
  } finally { client.close(); await fixture.close(); }
});

// 功能：验证非法消息和超限帧不会继续占用内存或挂住请求。
// 设计：在真实 TCP 上发送无效 JSON，再分块发送超限未终止帧触发读取端限制。
test('malformed and oversized frames close the connection', async () => {
  for (const oversized of [false, true]) {
    const fixture = await server((socket) => {
      if (!oversized) socket.write('invalid JSON\n');
      else {
        const block = Buffer.alloc(1024 * 1024, 120);
        for (let index = 0; index <= MAX_FRAME_BYTES / block.length; index++) socket.write(block);
      }
    });
    const client = await RpcClient.connect(fixture.port);
    try { await assert.rejects(client.request('read', {}), oversized ? /64 MB/ : /JSON/); }
    finally { client.close(); await fixture.close(); }
  }
});
