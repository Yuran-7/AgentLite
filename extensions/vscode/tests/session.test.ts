import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { ChatSession } from '../src/session';
import { decisions, parsePageMessage } from '../src/protocol';
import { waitFor } from './helpers';

class Client extends EventEmitter {
  calls: string[] = [];
  send?: () => Promise<Record<string, unknown>>;
  // 记录命令并以最小有效响应模拟 core 的异步返回行为。
  async request(method: string): Promise<Record<string, unknown>> {
    this.calls.push(method);
    if (method === 'session.create') return { session_id: 'session' };
    if (method === 'session.send_message' && this.send) return this.send();
    return { accepted: true, ok: true };
  }
  // 记录关闭行为，供生命周期测试断言。
  close(): void { this.calls.push('client.close'); }
}

// 功能：前端登记后持续续约，退出时注销，心跳失败后停止接受操作。
// 设计：用短心跳间隔观察真实计时器，验证隐藏 Webview 时宿主仍持有租约的机制。
test('frontend lease heartbeat, unregister, and heartbeat failure', async () => {
  const client = new Client(); const original = client.request.bind(client);
  let failing = false;
  client.request = async method => {
    if (method === 'frontend.register') {
      client.calls.push(method); return { managed: true, heartbeat_interval_s: 0.01 };
    }
    if (method === 'frontend.heartbeat' && failing) throw new Error('heartbeat failed');
    return original(method);
  };
  const session = new ChatSession('workspace', () => {});
  await session.attach(client, 'started');
  assert.equal(session.state.coreMode, 'managed');
  await waitFor(() => client.calls.includes('frontend.heartbeat'));
  await session.dispose();
  assert(client.calls.includes('frontend.unregister'));
  const calls = client.calls.length;
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(client.calls.length, calls);
  const other = new ChatSession('workspace', () => {});
  await other.attach(client, 'reused'); failing = true;
  await waitFor(() => other.state.connection === 'error');
  assert.equal(other.state.busy, false);
  await other.dispose();
});

// 功能：发送前完成订阅，早到的结束事件不会被迟到 RPC 响应重新激活。
// 设计：发送替身在返回前同步推送整轮事件，覆盖真实 core 的响应时序。
test('subscribe first; early events and late send response keep completed run idle', async () => {
  const client = new Client(); const session = new ChatSession('workspace', () => {});
  await session.attach(client, 'reused');
  assert.deepEqual(client.calls, ['frontend.register', 'session.create', 'event.subscribe']);
  client.send = async () => {
    client.emit('event', { type: 'run.started', session_id: 'session', run_id: 'run' });
    client.emit('event', { type: 'llm.token', run_id: 'run', token: '旧内容' });
    client.emit('event', { type: 'llm.token', run_id: 'run', reset: true, token: '' });
    client.emit('event', { type: 'llm.token', run_id: 'run', token: '最终回复' });
    client.emit('event', { type: 'run.finished', run_id: 'run', status: 'success' });
    client.emit('event', { type: 'session.waiting_for_input', session_id: 'session', last_run_id: 'run' });
    return { run_id: 'run' };
  };
  await session.send('hello');
  assert.equal(session.state.busy, false); assert.equal(session.state.runId, undefined);
  assert.equal(session.state.cards.find(card => card.kind === 'assistant')?.text, '最终回复');
  assert.equal(session.state.sending, false);
  await session.dispose(); assert(!client.calls.includes('core.shutdown'));
});

// 功能：停止必须等待 core 终止事件，断线后禁用工具与权限交互。
// 设计：故意延迟发送响应和结束事件，确认取消请求本身不会解锁输入。
test('cancel waits for terminal events; disconnect resolves pending cards', async () => {
  const client = new Client(); const session = new ChatSession('workspace', () => {});
  await session.attach(client, 'started'); let finish!: () => void;
  client.send = () => new Promise(resolve => { finish = () => resolve({ run_id: 'run' }); });
  const send = session.send('work');
  client.emit('event', { type: 'run.started', run_id: 'run', session_id: 'session' });
  await session.send('duplicate'); assert.equal(client.calls.filter(item => item === 'session.send_message').length, 1);
  await session.cancel(); assert.equal(session.state.busy, true); assert.equal(session.state.cancelling, true);
  session.event({ type: 'tool.call_started', run_id: 'run', tool_use_id: 'tool', tool_name: 'read_file', params: {} });
  session.event({ type: 'permission.requested', run_id: 'run', tool_use_id: 'perm', tool_name: 'write_file' });
  client.emit('disconnect', new Error('test disconnected'));
  assert.equal(session.state.connection, 'error');
  assert(session.state.cards.filter(card => card.kind === 'tool' || card.kind === 'permission').every(card => card.status === '中断'));
  finish(); await send; await session.dispose();
});

// 功能：四种权限决定只提交一次，超时事件关闭待审批按钮。
// 设计：分别驱动授权事件与外部拒绝事件，验证响应中的重复点击也被拒绝。
test('four permission decisions, repeated clicks, and external timeout', async () => {
  for (const decision of decisions) {
    const client = new Client(); const session = new ChatSession('workspace', () => {});
    await session.attach(client, 'reused');
    session.event({ type: 'run.started', run_id: 'run', session_id: 'session' });
    session.event({ type: 'permission.requested', run_id: 'run', tool_use_id: 'perm', tool_name: 'write_file' });
    await Promise.all([session.permission('perm', decision), session.permission('perm', decision)]);
    assert.equal(client.calls.filter(item => item === 'permission.respond').length, 1);
    assert.equal(session.state.cards[0].status, decision);
    session.event({ type: 'permission.requested', run_id: 'run', tool_use_id: 'timeout', tool_name: 'shell' });
    session.event({ type: 'permission.denied', run_id: 'run', tool_use_id: 'timeout', decision: 'auto_deny' });
    await session.permission('timeout', decision);
    assert.equal(client.calls.filter(item => item === 'permission.respond').length, 1);
    await session.dispose();
  }
});

// 功能：工具成功与失败、计划及子运行各自更新，陌生事件不串流。
// 设计：交错父子 token 与工具事件，并检查快照序列化足以恢复展示状态。
test('tool cards, plans, child streams, and snapshot restoration', async () => {
  const client = new Client(); const session = new ChatSession('workspace', () => {});
  await session.attach(client, 'reused');
  session.event({ type: 'run.started', run_id: 'parent', session_id: 'session' });
  session.event({ type: 'subagent.started', run_id: 'child', parent_run_id: 'parent', description: 'inspect' });
  session.event({ type: 'llm.token', run_id: 'parent', token: 'parent' });
  session.event({ type: 'llm.token', run_id: 'child', token: 'child' });
  session.event({ type: 'llm.token', run_id: 'stranger', token: 'wrong' });
  for (const failed of [false, true]) {
    const tool = failed ? 'failed' : 'ok';
    session.event({ type: 'tool.call_started', run_id: 'parent', tool_use_id: tool, tool_name: 'read_file', params: { path: 'x' } });
    session.event({ type: failed ? 'tool.call_failed' : 'tool.call_finished', run_id: 'parent', tool_use_id: tool, output: 'done', error_message: 'bad', elapsed_ms: 42 });
  }
  session.event({ type: 'plan.updated', run_id: 'parent', plan: [{ step: 'read', status: 'in_progress' }] });
  session.event({ type: 'plan.updated', run_id: 'parent', plan: [{ step: 'read', status: 'completed' }] });
  session.event({ type: 'subagent.finished', run_id: 'child', parent_run_id: 'parent', status: 'success' });
  const snapshot = JSON.parse(JSON.stringify(session.state));
  assert.deepEqual(snapshot.cards.filter((card: any) => card.kind === 'assistant').map((card: any) => card.text), ['parent', 'child']);
  assert.deepEqual(snapshot.cards.filter((card: any) => card.kind === 'tool').map((card: any) => card.status), ['success', 'failed']);
  assert.equal(snapshot.cards.find((card: any) => card.kind === 'plan').plan[0].status, 'completed');
  await session.dispose();
});

// 功能：拒绝页面透传命令及非法权限决定。
// 设计：从不受信任消息边界检查有效操作、超限文本与任意 RPC 注入。
test('webview message allowlist', () => {
  assert.equal(parsePageMessage({ type: 'core.shutdown' }), undefined);
  assert.equal(parsePageMessage({ type: 'permission', toolUseId: 'x', decision: 'allow_everything' }), undefined);
  assert.equal(parsePageMessage({ type: 'send', content: 'x'.repeat(1_000_001) }), undefined);
  assert.deepEqual(parsePageMessage({ type: 'send', content: ' hello ' }), { type: 'send', content: 'hello' });
});
