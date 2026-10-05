import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { ChatSession, historyCards } from '../src/session';
import { decisions, parsePageMessage } from '../src/protocol';
import { waitFor } from './helpers';

// 功能：停止时无模型回答也会产生带完成标记的活动摘要。
// 设计：驱动真实 run.finished 事件，保证页面获得独立于回答卡片的折叠依据。
test('cancelled run without an answer supplies a completed notice', async t => {
  const client = new Client(); const session = new ChatSession('workspace', () => {}, client);
  t.after(() => session.dispose()); Object.assign(session.state, { sessionId: 'session', connection: 'ready' });
  session.event({ type: 'run.started', run_id: 'r', session_id: 'session' });
  session.event({ type: 'tool.call_started', run_id: 'r', tool_use_id: 't', tool_name: 'read_file', params: {} });
  session.event({ type: 'run.finished', run_id: 'r', status: 'failed', reason: 'cancelled' });
  const notice = session.state.cards.find(card => card.kind === 'notice');
  assert.equal(notice?.text, '已停止'); assert(notice?.completed); assert.equal(typeof notice?.workMs, 'number');
  assert.equal(session.state.cards.find(card => card.kind === 'tool')?.status, '已取消');
});

// 功能：计划模式切换、问答和执行使用独立 RPC，防止重复回答。
// 设计：驱动真实会话类，覆盖运行期间模式锁定、答案失败重试和计划执行转换。
test('plan mode questions and implementation flow', async t => {
  const client = new Client(); const session = new ChatSession('workspace', () => {}, client);
  t.after(() => session.dispose());
  Object.assign(session.state, { sessionId: 'session', connection: 'ready' });
  await session.collaboration('plan'); assert.equal(session.state.collaborationMode, 'plan');
  session.state.busy = true;
  await session.collaboration('default'); assert.equal(session.state.collaborationMode, 'plan');
  session.state.cards.push({ id: 'q', kind: 'question', text: '', status: 'pending', requestId: 'request' });
  await session.answerQuestions('request', { scope: 'custom' });
  await session.answerQuestions('request', { scope: 'custom' });
  assert.equal(client.calls.filter(call => call === 'user_input.respond').length, 1);
  session.state.busy = false;
  session.state.cards.push({ id: 'p', kind: 'assistant', text: '<proposed_plan>\n# Plan\nImplement feature\n</proposed_plan>', completed: true });
  await session.implementPlan('p');
  assert.equal(session.state.collaborationMode, 'default');
  assert(client.calls.includes('session.send_message'));
  assert.equal(parsePageMessage({ type: 'answerQuestions', requestId: 'r', answers: { a: '' } }), undefined);
  assert.equal(parsePageMessage({ type: 'collaboration', mode: 'invalid' }), undefined);
});

// 多模态工具历史只展示文字摘要，不将图片引用序列化为实现细节。
test('history tool results render PDF text summaries', () => {
  const cards = historyCards([
    { role: 'assistant', content: [{ type: 'tool_use', id: 'pdf', name: 'read_file', input: { path: 'paper.pdf' } }] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'pdf', content: [
      { type: 'text', text: 'PDF paper.pdf, page 1\nImage: /assets/page-1.jpg' },
      { type: 'image', source: { type: 'file', path: '/assets/page-1.jpg' } },
    ] }] },
  ]);
  assert.equal(cards[0].output, 'PDF paper.pdf, page 1\nImage: /assets/page-1.jpg');
  assert.equal(cards[0].status, 'success');
});

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

// 功能：文件引用使代理先读取指定相对路径，同时历史卡片只显示用户原文。
// 设计：捕获真实会话发送参数并重放历史消息，覆盖引用提示与展示分离。
test('workspace file references reach the agent and stay out of history cards', async t => {
  const client = new Client(); const session = new ChatSession('workspace', () => {}, client);
  t.after(() => session.dispose());
  Object.assign(session.state, { sessionId: 'session', connection: 'ready' });
  let sent = '';
  client.request = async (method: string, params?: Record<string, unknown>) => {
    if (method === 'session.send_message') sent = String(params?.content);
    return { accepted: true };
  };
  await session.send('请看 @src/main.ts @src/', [], ['src/main.ts', 'src/']);
  assert.match(sent, /read_file/); assert.match(sent, /list_dir/);
  assert.match(sent, /"src\/main.ts"/); assert.match(sent, /"src\/"/);
  assert.equal(session.state.cards.find(card => card.kind === 'user')?.text, '请看 @src/main.ts @src/');
  assert.deepEqual(session.state.cards.find(card => card.kind === 'user')?.references, ['src/main.ts', 'src/']);
  assert.equal(historyCards([{ role: 'user', content: sent }])[0].text, '请看 @src/main.ts @src/');
  assert.deepEqual(historyCards([{ role: 'user', content: sent }])[0].references, ['src/main.ts', 'src/']);
});

// 功能：推理档位读取和修改互斥执行，失败不覆盖已有选择。
// 设计：延迟真实会话控制路径的 RPC 替身，覆盖重复点击、运行锁定与旧 core。
test('reasoning reads, updates and rejects concurrent operations', async t => {
  const client = new Client(); const session = new ChatSession('workspace', () => {}, client);
  t.after(() => session.dispose());
  Object.assign(session.state, { sessionId: 'session', connection: 'ready', reasoningEffort: 'medium' });
  let finish!: (result: Record<string, unknown>) => void;
  const calls: Record<string, unknown>[] = [];
  client.request = async (method: string, params?: Record<string, unknown>) => {
    assert.equal(method, 'session.reasoning'); calls.push(params!);
    return new Promise(resolve => { finish = resolve; });
  };
  const pending = session.reasoning('high');
  await session.reasoning('low'); assert.equal(calls.length, 1); assert(session.state.sending);
  assert.deepEqual(calls[0], { session_id: 'session', effort: 'high' });
  finish({ effort: 'high', effective_effort: 'high', supported: true, model: 'test', efforts: ['low', 'high', 'untrusted'] }); await pending;
  assert.deepEqual(session.state.reasoningOptions, { supported: true, model: 'test', efforts: ['low', 'high'], effectiveEffort: 'high' });
  assert(!session.state.reasoningLoading);
  assert.equal(session.state.reasoningEffort, 'high'); assert(!session.state.sending);
  assert.equal(session.state.cards.length, 0);
  session.state.busy = true; await session.reasoning('low'); assert.equal(calls.length, 1);
  session.state.busy = false;
  client.request = async () => { throw new Error('Method not found'); };
  await session.reasoning('low'); assert(session.state.error?.includes('重启 core'));
  assert.equal(session.state.reasoningEffort, 'high'); assert(!session.state.sending);
  assert.deepEqual(parsePageMessage({ type: 'reasoning', effort: '' }), { type: 'reasoning', effort: '' });
  assert.equal(parsePageMessage({ type: 'reasoning', effort: 'invalid' }), undefined);
});

// 功能：当前会话的记忆开关独立保存，失败时保留服务端已确认的状态。
// 设计：拦截 session.set_memory 响应，覆盖并发点击、返回值同步和旧 core 报错。
test('memory settings persist independently through the core', async t => {
  const client = new Client(); const session = new ChatSession('workspace', () => {}, client);
  t.after(() => session.dispose());
  Object.assign(session.state, { sessionId: 'session', connection: 'ready' });
  const calls: Record<string, unknown>[] = [];
  let finish!: (result: Record<string, unknown>) => void;
  client.request = async (method: string, params?: Record<string, unknown>) => {
    assert.equal(method, 'session.set_memory'); calls.push(params!);
    return new Promise(resolve => { finish = resolve; });
  };
  const pending = session.setMemory('generate', true);
  await session.setMemory('use', false);
  assert.deepEqual(calls, [{ session_id: 'session', generate_enabled: true }]);
  assert(session.state.memoryLoading && session.state.sending);
  finish({ generate_enabled: true, use_enabled: true }); await pending;
  assert.equal(session.state.memoryGenerateEnabled, true);
  assert.equal(session.state.memoryUseEnabled, true);
  assert(!session.state.memoryLoading && !session.state.sending);
  client.request = async () => { throw new Error('Method not found'); };
  await session.setMemory('use', false);
  assert.equal(session.state.memoryUseEnabled, true);
  assert(session.state.memoryError?.includes('重启 core'));
  assert.deepEqual(parsePageMessage({ type: 'setMemory', setting: 'use', enabled: false }),
    { type: 'setMemory', setting: 'use', enabled: false });
  assert.equal(parsePageMessage({ type: 'setMemory', setting: 'use', enabled: 'false' }), undefined);
});

// 功能：空推理设置写入真实默认值，DeepSeek 只显示有效档位且不产生聊天提示。
// 设计：模拟旧 core 返回通用七档，截获二次 RPC 验证默认设置确实保存。
test('reasoning persists model defaults and narrows DeepSeek levels', async t => {
  for (const [model, expected, levels] of [
    ['gpt-6.1-sol', 'medium', ['low', 'medium', 'high', 'xhigh', 'max']],
    ['deepseek-flash', 'high', ['none', 'low', 'high', 'max']],
  ] as const) {
    const client = new Client(); const session = new ChatSession('workspace', () => {}, client);
    t.after(() => session.dispose());
    Object.assign(session.state, { sessionId: 'session', connection: 'ready' });
    const calls: Record<string, unknown>[] = [];
    client.request = async (_method: string, params?: Record<string, unknown>) => {
      calls.push(params!);
      return { model, supported: true, effort: params?.effort ?? '', effective_effort: params?.effort ?? '',
        efforts: model.startsWith('deepseek') ? ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] : [...levels] };
    };
    await session.reasoning();
    assert.deepEqual(calls, [{ session_id: 'session' }, { session_id: 'session', effort: expected }]);
    assert.equal(session.state.reasoningEffort, expected);
    assert.deepEqual(session.state.reasoningOptions?.efforts, [...levels]);
    assert.equal(session.state.cards.length, 0);
  }
});

test('MCP state stays separate from chat errors, serializes changes and reports old cores', async t => {
  const client = new Client(); const session = new ChatSession('workspace', () => {});
  t.after(() => session.dispose()); await session.attach(client, 'reused');
  let mutationFinished: (() => void) | undefined;
  const messages: { method: string; params: Record<string, unknown> }[] = [];
  client.request = async (method: string, params?: Record<string, unknown>) => {
    messages.push({ method, params: params ?? {} });
    if (method === 'mcp.manage') await new Promise<void>(resolve => { mutationFinished = resolve; });
    return { servers: [{ name: 'demo', status: 'connected', enabled: true, transport: 'stdio', tools: [] }], settingsPath: 'mcp.json' };
  };
  await session.refreshMcp(); assert.equal(session.state.mcpServers?.[0].name, 'demo');
  const pending = session.manageMcp('set_enabled', { name: 'demo', enabled: false });
  assert(session.state.mcpBusy);
  await session.manageMcp('reconnect', { name: 'demo' }); await session.send('should wait');
  assert.equal(messages.filter(message => message.method === 'mcp.manage').length, 1);
  assert(!messages.some(message => message.method === 'session.send_message'));
  mutationFinished!(); await pending; assert(!session.state.mcpBusy);
  assert.deepEqual(messages.at(-1)?.params, { action: 'set_enabled', name: 'demo', enabled: false });
  session.state.busy = true;
  const count = messages.length; await session.manageMcp('reload'); assert.equal(messages.length, count);
  await session.refreshMcp(); assert.equal(messages.length, count + 1);
  session.state.busy = false;
  client.request = async () => { throw new Error('Method not found: mcp.list'); };
  await session.refreshMcp(); assert(session.state.mcpError?.includes('重启 core')); assert.equal(session.state.error, undefined);
  assert(!session.state.mcpLoading);
  await session.manageMcp('reload'); assert(!session.state.mcpBusy);
});

// 功能：压缩当前会话互斥执行，保留展示记录，失败后恢复输入并保留占用。
// 设计：延迟 RPC 替身覆盖双击与运行中操作，再验证成功与失败两条状态路径。
test('manual compaction preserves cards and serializes actions with accurate usage state', async () => {
  const client = new Client(); const session = new ChatSession('workspace', () => {}, client);
  Object.assign(session.state, { sessionId: 'session', connection: 'ready', contextPercent: 62, usage: '62%' });
  session.state.cards.push({ id: 'answer', kind: 'assistant', text: 'keep this answer' });
  session.state.runId = 'run'; session.event({ type: 'llm.usage', run_id: 'run', context_pct: .62 });
  assert.equal(session.state.contextPercent, 62); session.state.runId = undefined;
  let finish!: (result: Record<string, unknown>) => void;
  let calls = 0;
  client.request = async method => {
    if (method !== 'session.compact') return {};
    calls++; return new Promise(resolve => { finish = resolve; });
  };
  session.state.busy = true; await session.compactSession(); assert.equal(calls, 0); session.state.busy = false;
  const pending = session.compactSession(); await session.compactSession();
  assert.equal(calls, 1); assert(session.state.compacting && session.state.sending);
  finish({ saved_tokens: 2000, summary_tokens: 500 }); await pending;
  assert.equal(session.state.cards[0].text, 'keep this answer');
  assert(session.state.cards.at(-1)?.text.includes('2,000'));
  assert.equal(session.state.contextPercent, 62); assert(!session.state.compacting && !session.state.sending);
  session.state.contextPercent = 40;
  client.request = async () => { throw new Error('not beneficial'); };
  await session.compactSession(); assert.equal(session.state.contextPercent, 40);
  assert(session.state.error?.includes('not beneficial')); assert(!session.state.sending);
});

// 功能：真实连接等待被计入耗时，成功后心跳和新会话不会改变结果，重连重新计时。
// 设计：控制宿主时钟和订阅响应，明确覆盖 core 就绪前等待与错误终止的边界。
test('connection timing includes startup and freezes at session readiness', async t => {
  let now = 100_000;
  t.mock.method(Date, 'now', () => now);
  t.mock.method(performance, 'now', () => now);
  const client = new Client(); const original = client.request.bind(client);
  client.request = async method => {
    if (method === 'event.subscribe') now += 200;
    if (method === 'session.list') now += 1000;
    return original(method);
  };
  const session = new ChatSession('workspace', () => {});
  session.beginConnection(); now += 7500;
  await session.attach(client, 'started');
  assert.equal(session.state.connectionStartedAt, 100_000);
  assert.equal(session.state.connectionElapsedMs, 7700);
  now += 5000; await session.newSession();
  assert.equal(session.state.connectionElapsedMs, 7700);
  session.fail('disconnected'); assert.equal(session.state.connectionElapsedMs, 7700);
  session.beginConnection();
  assert.equal(session.state.connectionElapsedMs, undefined);
  const retryAt = now; now += 2500;
  session.fail('startup failed');
  assert.equal(session.state.connectionStartedAt, retryAt);
  assert.equal(session.state.connectionElapsedMs, 2500);
  await session.dispose();
});

// 功能：耗时来自当前主运行，完成后附在最终回答，失败和切换清除运行计时。
// 设计：模拟时钟与实际事件顺序，覆盖中途多个回答块以及迟到 RPC 响应。
test('work duration and message timestamps reflect the real main run', async t => {
  let now = 1_000_000; t.mock.method(Date, 'now', () => now);
  const client = new Client(); const session = new ChatSession('workspace', () => {});
  await session.attach(client, 'reused');
  client.send = async () => {
    now += 1000; client.emit('event', { type: 'run.started', run_id: 'main' });
    client.emit('event', { type: 'llm.token', run_id: 'main', token: '正在检查' });
    client.emit('event', { type: 'step.started', run_id: 'main' });
    client.emit('event', { type: 'llm.token', run_id: 'main', token: '最终回答' });
    now += 260_000;
    client.emit('event', { type: 'run.finished', run_id: 'main', status: 'success' });
    client.emit('event', { type: 'session.waiting_for_input', last_run_id: 'main' });
    return {};
  };
  await session.send('hello');
  const answers = session.state.cards.filter(card => card.kind === 'assistant');
  assert.equal(answers[0].workMs, undefined); assert.equal(answers[1].workMs, 261_000);
  assert(session.state.cards[0].createdAt); assert.equal(session.state.workStartedAt, undefined);
  assert(!session.state.cards.some(card => card.text === '已完成'));
  session.state.workStartedAt = now; session.fail('lost'); assert.equal(session.state.workStartedAt, undefined);
  await session.dispose();
});

test('a new current session can be renamed before it appears in history', async () => {
  const client = new Client(); const session = new ChatSession('workspace', () => {});
  await session.attach(client, 'reused');
  assert.equal(session.state.history?.length, 0);
  await session.renameSession('session', 'My new chat');
  assert(client.calls.includes('session.rename')); assert.equal(session.state.title, 'My new chat');
  const count = client.calls.length;
  await session.renameSession('unknown', 'No'); assert.equal(client.calls.length, count);
  session.state.busy = true; await session.renameSession('session', 'Busy');
  assert.equal(client.calls.length, count); assert.equal(session.state.title, 'My new chat');
  session.state.busy = false; await session.dispose();
});

test('resume restores transcript and model, filters stale events and persists rename through RPC', async () => {
  const client = new Client();
  const paramsSeen: { method: string; params: Record<string, unknown> }[] = [];
  const transport = Object.assign(client, { request: async (method: string, params: Record<string, unknown>) => {
    paramsSeen.push({ method, params });
    if (method === 'session.create') return { session_id: 'new' };
    if (method === 'session.list') return { sessions: [{ session_id: 'old', title: 'Old chat', updated_at: '2026-10-01' }] };
    if (method === 'model.list') return { models: [{ id: 'custom', model: 'test', protocol: 'openai' }], defaultModel: 'custom' };
    if (method === 'session.resume') return { title: 'Old chat', model_id: 'custom',
      memory_generate_enabled: true, memory_use_enabled: false };
    if (method === 'session.get_history') return { messages: [
      { role: 'user', content: 'question' }, { role: 'assistant', content: [{ type: 'text', text: 'answer' }] }
    ], last_usage: { context_pct: .62, context_window_estimated: true, total_input_tokens: 62000, output_tokens: 500 } };
    return {};
  } });
  const session = new ChatSession('workspace', () => {});
  await session.attach(transport, 'reused');
  await session.resumeSession('old');
  assert.equal(session.state.sessionId, 'old');
  assert.equal(session.state.selectedModel, 'custom');
  assert.equal(session.state.memoryGenerateEnabled, true);
  assert.equal(session.state.memoryUseEnabled, false);
  assert.equal(session.state.contextPercent, 62);
  assert(session.state.contextEstimated); assert(session.state.usage.includes('上下文 ≈62.0%'));
  assert.deepEqual(session.state.cards.map(card => card.text), ['question', 'answer']);
  const close = paramsSeen.findIndex(call => call.method === 'session.close');
  const subscribe = paramsSeen.findLastIndex(call => call.method === 'event.subscribe');
  assert(close > subscribe);
  session.event({ type: 'llm.token', session_id: 'new', run_id: 'stale', token: 'ignored' });
  assert.equal(session.state.cards.length, 2);
  await session.renameSession('old', 'Renamed');
  assert(paramsSeen.some(call => call.method === 'session.rename' && call.params.title === 'Renamed'));
  await session.selectModel(''); assert.equal(session.state.selectedModel, '');
  session.state.busy = true;
  const count = paramsSeen.length; await session.resumeSession('old'); await session.selectModel('custom');
  assert.equal(paramsSeen.length, count);
  session.state.busy = false; await session.dispose();
});

test('historical tools show results without replaying permissions', () => {
  const cards = historyCards([
    { role: 'assistant', content: [{ type: 'tool_use', id: 't', name: 'shell', input: { command: 'ls' } }] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't', content: 'file.txt' }] },
    { role: 'assistant', content: [{ type: 'text', text: 'done' }] }
  ]);
  assert.equal(cards[0].kind, 'tool'); assert.equal(cards[0].output, 'file.txt');
  assert.equal(cards[0].status, 'success'); assert(!cards.some(card => card.kind === 'permission'));
});

test('old core model errors explain restart and clear after a successful refresh', async () => {
  const client = new Client(); const original = client.request.bind(client);
  let oldCore = true;
  client.request = async method => {
    if (method === 'model.list') {
      if (oldCore) throw new Error('Method not found: model.list');
      return { models: [{ id: 'custom', name: 'My model', model: 'test', protocol: 'openai' }],
        defaultModel: 'custom', fallbackModel: { id: '', name: 'existing-model', model: 'existing-model', protocol: 'anthropic' } };
    }
    return original(method);
  };
  const session = new ChatSession('workspace', () => {});
  await session.attach(client, 'reused');
  assert(session.state.error?.includes('重启 core'));
  oldCore = false; await session.refreshModels();
  assert.equal(session.state.error, undefined); assert.equal(session.state.selectedModel, 'custom');
  assert.equal(session.state.fallbackModel?.name, 'existing-model');
  await session.dispose();
});

// 功能：前端登记后持续续约，退出时注销，心跳失败后停止接受操作。
// 设计：用短心跳间隔观察真实计时器，验证隐藏 Webview 时宿主仍持有租约的机制。
test('frontend lease heartbeat, unregister, and heartbeat failure', async () => {
  const client = new Client(); const original = client.request.bind(client);
  let failing = false;
  client.request = async method => {
    if (method === 'frontend.register') {
      client.calls.push(method); return { managed: true, heartbeat_interval_s: 0.01,
        frontend_counts: { vscode: 1, tui: 0 } };
    }
    if (method === 'frontend.heartbeat' && failing) throw new Error('heartbeat failed');
    if (method === 'frontend.heartbeat') {
      client.calls.push(method); return { managed: true, frontend_counts: { vscode: 2, tui: 1 } };
    }
    return original(method);
  };
  const session = new ChatSession('workspace', () => {});
  await session.attach(client, 'started');
  assert.equal(session.state.coreMode, 'managed');
  assert.deepEqual(session.state.frontendCounts, { vscode: 1, tui: 0 });
  await waitFor(() => session.state.frontendCounts?.tui === 1);
  assert.deepEqual(session.state.frontendCounts, { vscode: 2, tui: 1 });
  await session.dispose();
  assert(client.calls.includes('frontend.unregister'));
  const calls = client.calls.length;
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(client.calls.length, calls);
  const other = new ChatSession('workspace', () => {});
  await other.attach(client, 'reused'); failing = true;
  await waitFor(() => other.state.connection === 'error');
  assert.equal(other.state.busy, false);
  assert.equal(other.state.frontendCounts, undefined);
  await other.dispose();
});

// 功能：发送前完成订阅，早到的结束事件不会被迟到 RPC 响应重新激活。
// 设计：发送替身在返回前同步推送整轮事件，覆盖真实 core 的响应时序。
test('subscribe first; early events and late send response keep completed run idle', async () => {
  const client = new Client(); const session = new ChatSession('workspace', () => {});
  await session.attach(client, 'reused');
  assert.deepEqual(client.calls, ['frontend.register', 'session.create', 'event.subscribe', 'session.list', 'model.list']);
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
  assert.equal(parsePageMessage({ type: 'send', content: '@file', references: ['x'.repeat(1025)] }), undefined);
  assert.equal(parsePageMessage({ type: 'searchWorkspaceFiles', query: 'x'.repeat(201), requestId: 1 }), undefined);
  assert.deepEqual(parsePageMessage({ type: 'send', content: ' hello ' }), { type: 'send', content: 'hello' });
});

// 功能：usage 比例显示为百分数，读取归一化输入，子任务不会改写主水位。
// 设计：重放截图数值、缓存独立字段与多次请求，避免累计或重复加缓存。
test('usage displays normalized input and percentage of latest main request', async () => {
  const client = new Client(); const session = new ChatSession('workspace', () => {});
  await session.attach(client, 'reused');
  client.emit('event', { type: 'run.started', run_id: 'main' });
  client.emit('event', { type: 'llm.usage', run_id: 'main', input_tokens: 21024,
    total_input_tokens: 21024, output_tokens: 422, cache_read_input_tokens: 19968,
    context_pct: 21446 / 200000 });
  assert.equal(session.state.usage, '输入 21024 · 输出 422 · 缓存 19968 · 上下文 10.7%');
  client.emit('event', { type: 'llm.usage', run_id: 'main', input_tokens: 1000,
    total_input_tokens: 3000, output_tokens: 200, cache_read_input_tokens: 1500,
    cache_creation_input_tokens: 500, context_pct: 0.016, context_window_estimated: true });
  assert.equal(session.state.usage, '输入 3000 · 输出 200 · 缓存 1500 · 上下文 ≈1.6%');
  client.emit('event', { type: 'subagent.started', run_id: 'child', parent_run_id: 'main', description: 'child' });
  client.emit('event', { type: 'llm.usage', run_id: 'child', input_tokens: 100000, context_pct: 0.9 });
  assert.equal(session.state.usage, '输入 3000 · 输出 200 · 缓存 1500 · 上下文 ≈1.6%');
  await session.dispose();
});


// 功能：权限模式可在运行中切换，退出 Plan 使用一次原子 RPC，跨客户端事件同步模式。
// 设计：捕获真实 ChatSession 的请求与状态，验证失败时不提前修改本地选择。
test('permission mode updates while busy and exits plan atomically', async t => {
  const client = new Client(); const session = new ChatSession('workspace', () => {}, client);
  t.after(() => session.dispose());
  Object.assign(session.state, { sessionId: 'session', connection: 'ready', busy: true, permissionMode: 'manual' });
  const calls: { method: string; params?: Record<string, unknown> }[] = [];
  client.request = async (method: string, params?: Record<string, unknown>) => {
    calls.push({ method, params });
    return { mode: params?.mode, permission_mode: params?.permission_mode };
  };
  await session.permissionMode('auto');
  assert.equal(session.state.permissionMode, 'auto');
  assert.equal(calls[0].method, 'session.permission_mode');
  session.state.busy = false; await session.collaboration('plan');
  await session.permissionMode('accept_edits');
  assert.deepEqual(calls.at(-1), { method: 'session.collaboration', params: {
    session_id: 'session', mode: 'default', permission_mode: 'accept_edits',
  }});
  session.event({ type: 'session.mode_changed', session_id: 'session', permission_mode: 'manual', collaboration_mode: 'plan' });
  assert.equal(session.state.permissionMode, 'manual');
  assert.equal(session.state.collaborationMode, 'plan');
  client.request = async () => { throw new Error('busy'); };
  await session.permissionMode('auto');
  assert.equal(session.state.permissionMode, 'manual');
  assert.equal(session.state.collaborationMode, 'plan');
});

// 功能：审批原因与限定选项保留在卡片上，自动批准标记显示在对应工具。
// 设计：重放真实事件顺序并检查协议拒绝非法模式，兼容未知批准决策。
test('auto approval events carry reasons, choices and tool badge', t => {
  const client = new Client(); const session = new ChatSession('workspace', () => {}, client);
  t.after(() => session.dispose()); session.state.sessionId = 'session';
  session.event({ type: 'run.started', run_id: 'r', session_id: 'session' });
  session.event({ type: 'tool.call_started', run_id: 'r', tool_use_id: 't', tool_name: 'shell', params: {} });
  session.event({ type: 'permission.requested', run_id: 'r', tool_use_id: 't', tool_name: 'shell',
    reason: '分类器超时', allowed_decisions: ['allow_once', 'deny_once', 'always_deny'] });
  const card = session.state.cards.find(card => card.kind === 'permission')!;
  assert.equal(card.approvalReason, '分类器超时');
  assert(!card.allowedDecisions?.includes('always_allow'));
  session.event({ type: 'permission.granted', run_id: 'r', tool_use_id: 't', decision: 'classifier_allow' });
  assert(session.state.cards.find(card => card.kind === 'tool')?.autoApproved);
  assert.deepEqual(parsePageMessage({ type: 'permissionMode', mode: 'auto' }), { type: 'permissionMode', mode: 'auto' });
  assert.equal(parsePageMessage({ type: 'permissionMode', mode: 'plan' }), undefined);
});
