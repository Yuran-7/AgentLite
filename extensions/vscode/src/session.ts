import { randomUUID } from 'node:crypto';
import type { CoreEvent, Decision } from './protocol';

export type Card = {
  id: string; kind: 'user' | 'assistant' | 'tool' | 'permission' | 'plan' | 'notice' | 'subagent';
  runId?: string; text: string; title?: string; status?: string; params?: unknown;
  output?: string; elapsedMs?: number; toolUseId?: string;
  plan?: { step: string; status: string }[];
};
export type ChatState = {
  workspace: string; connection: 'disconnected' | 'connecting' | 'ready' | 'error';
  origin?: 'reused' | 'started'; sessionId?: string; runId?: string;
  coreMode?: 'managed' | 'persistent';
  busy: boolean; sending: boolean; cancelling: boolean; error?: string;
  model: string; usage: string; cards: Card[];
};
export interface SessionClient {
  request(method: string, params: Record<string, unknown>, timeoutMs?: number): Promise<Record<string, unknown>>;
  on(name: 'event', listener: (event: CoreEvent) => void): unknown;
  on(name: 'disconnect', listener: (error: Error) => void): unknown;
  close(): void;
}

export class ChatSession {
  readonly state: ChatState;
  private streams = new Map<string, Card>();
  private children = new Set<string>();
  private timer?: NodeJS.Timeout;
  private heartbeat?: NodeJS.Timeout;
  private disposed = false;
  private epoch = 0;

  // 在扩展宿主维护完整展示状态，页面重建时无需重新订阅或重放。
  constructor(workspace: string, private readonly changed: (state: ChatState) => void,
    private client?: SessionClient) {
    this.state = { workspace, connection: 'disconnected', busy: false, sending: false,
      cancelling: false, model: '', usage: '', cards: [] };
  }

  // 发布连接中的状态并隐藏上一条连接错误。
  connecting(): void {
    this.state.connection = 'connecting'; this.state.error = undefined; this.publish();
  }

  // 绑定一次连接并在允许发送前完成会话创建和定向订阅。
  async attach(client: SessionClient, origin: 'reused' | 'started'): Promise<void> {
    if (this.disposed) { client.close(); return; }
    this.client = client;
    this.state.origin = origin;
    client.on('event', event => { if (!this.disposed) this.event(event); });
    client.on('disconnect', error => {
      clearInterval(this.heartbeat);
      if (!this.disposed) this.fail(error.message);
    });
    try {
      const lease = await client.request('frontend.register', { client: 'vscode' }, 5000);
      this.state.coreMode = lease.managed ? 'managed' : 'persistent';
      if (this.disposed) { client.close(); return; }
      const interval = Number(lease.heartbeat_interval_s || 10) * 1000;
      this.heartbeat = setInterval(() => {
        void client.request('frontend.heartbeat', {}, 5000).then(updated => {
          this.state.coreMode = updated.managed ? 'managed' : 'persistent'; this.publish();
        }).catch(error => {
          clearInterval(this.heartbeat);
          this.fail(this.errorText(error)); client.close();
        });
      }, interval);
      this.heartbeat.unref();
    } catch (error) {
      client.close();
      throw new Error(`前端登记失败，请停止旧 core 后重试：${this.errorText(error)}`);
    }
    try { await this.newSession(); }
    catch (error) { clearInterval(this.heartbeat); client.close(); throw error; }
  }

  // 原子切换到已订阅的新会话，失败后禁用发送以防误用旧订阅。
  async newSession(): Promise<void> {
    if (!this.client || this.state.busy || this.state.sending || this.disposed) return;
    this.connecting();
    const previous = this.state.sessionId;
    const created = await this.client.request('session.create', {
      mode: 'chat', workspace_root: this.state.workspace, title: 'VS Code'
    });
    const id = String(created.session_id);
    try {
      await this.client.request('event.subscribe', {
        topics: ['session.*', 'run.*', 'step.*', 'llm.*', 'tool.*', 'permission.*', 'plan.*', 'subagent.*', 'log.*'],
        scope: `session:${id}`
      });
    } catch (error) {
      await this.client.request('session.close', { session_id: id }, 1500).catch(() => {});
      throw error;
    }
    if (this.disposed || this.state.connection === 'error') return;
    this.epoch++;
    this.state.sessionId = id; this.state.connection = 'ready'; this.state.cards = [];
    this.state.model = ''; this.state.usage = ''; this.state.runId = undefined;
    this.streams.clear(); this.children.clear(); this.publish();
    if (previous) await this.client.request('session.close', { session_id: previous }, 1500).catch(() => {});
  }

  // 发送任务时锁定输入，等待整轮 RPC 响应但让事件立即驱动显示。
  async send(content: string): Promise<void> {
    if (!this.client || this.state.connection !== 'ready' || this.state.busy || this.state.sending || !content.trim()) return;
    const epoch = this.epoch;
    this.state.busy = true; this.state.sending = true; this.state.error = undefined;
    this.add('user', content); this.publish();
    try {
      await this.client.request('session.send_message', { session_id: this.state.sessionId, content }, 0);
      // core 的响应晚于 run.finished；绝不从响应重新激活已结束的 run。
    } catch (error) {
      if (epoch === this.epoch && !this.disposed) {
        this.state.busy = false; this.state.runId = undefined; this.state.cancelling = false;
        this.resolvePending('中断'); this.state.error = this.errorText(error);
      }
    } finally {
      if (epoch === this.epoch && !this.disposed) { this.state.sending = false; this.publish(); }
    }
  }

  // 发出停止请求后继续等待 core 的 run.finished 与 waiting_for_input。
  async cancel(): Promise<void> {
    if (!this.client || !this.state.runId || !this.state.busy || this.state.cancelling) return;
    this.state.cancelling = true; this.publish();
    try {
      const result = await this.client.request('session.cancel', {
        session_id: this.state.sessionId, run_id: this.state.runId
      });
      if (!result.accepted && this.state.busy) {
        this.state.cancelling = false; this.publish();
      }
    } catch (error) { this.state.cancelling = false; this.report(error); }
  }

  // 只响应宿主当前仍等待决定的权限，按钮立即进入等待状态。
  async permission(toolUseId: string, decision: Decision): Promise<void> {
    const card = this.state.cards.find(item => item.kind === 'permission' && item.toolUseId === toolUseId);
    if (!this.client || this.state.connection !== 'ready' || card?.status !== 'pending') return;
    card.status = 'responding'; this.publish();
    try {
      await this.client.request('permission.respond', { tool_use_id: toolUseId, decision });
      if (card.status === 'responding') card.status = decision;
      this.publish();
    } catch (error) {
      if (card.status === 'responding') card.status = 'pending';
      this.report(error);
    }
  }

  // 将连接失败与未完成的工具、权限统一标为中断。
  fail(message: string): void {
    this.state.connection = 'error'; this.state.error = message;
    this.state.busy = false; this.state.sending = false; this.state.cancelling = false;
    this.state.runId = undefined; this.resolvePending('中断'); this.streams.clear(); this.publish();
  }

  // 将业务错误显示到页面而不破坏可用的连接。
  report(error: unknown): void { this.state.error = this.errorText(error); this.publish(); }

  // 清理自己的任务与会话，但不停止共享 core。
  async dispose(): Promise<void> {
    if (this.disposed) return;
    this.disposed = true; this.epoch++; clearTimeout(this.timer);
    clearInterval(this.heartbeat);
    if (this.client) {
      try {
        if (this.state.runId && this.state.busy) await this.client.request('session.cancel', {
          session_id: this.state.sessionId, run_id: this.state.runId
        }, 1500).catch(() => {});
        if (this.state.sessionId) await this.client.request('session.close', {
          session_id: this.state.sessionId
        }, 1500).catch(() => {});
      } finally {
        await this.client.request('frontend.unregister', {}, 1000).catch(() => {});
        this.client.close();
      }
    }
  }

  // 按 run 与工具标识更新卡片，父子运行各自拥有独立文本块。
  event(event: CoreEvent): void {
    const type = event.type;
    const runId = typeof event.run_id === 'string' ? event.run_id : '';
    if (event.session_id && event.session_id !== this.state.sessionId) return;
    if (type === 'run.started') {
      if (!this.children.has(runId)) { this.state.runId = runId; this.state.busy = true; }
    } else if (type === 'subagent.started') {
      if (event.parent_run_id !== this.state.runId && !this.children.has(String(event.parent_run_id))) return;
      this.children.add(runId);
      Object.assign(this.add('subagent', String(event.description ?? ''), runId), { status: 'running', title: '子 Agent' });
    } else if (runId && runId !== this.state.runId && !this.children.has(runId) &&
      type !== 'session.waiting_for_input') return;

    if (type === 'llm.token') {
      let card = this.streams.get(runId);
      if (event.reset) { if (card) card.text = ''; }
      else {
        if (!card) { card = this.add('assistant', '', runId); this.streams.set(runId, card); }
        card.text += String(event.token ?? '');
      }
      this.schedule(); return;
    }
    if (['step.started', 'tool.call_started', 'plan.updated', 'run.finished', 'subagent.finished'].includes(type)) {
      this.streams.delete(runId);
    }
    const toolId = String(event.tool_use_id ?? '');
    if (type === 'tool.call_started') {
      Object.assign(this.add('tool', '', runId), { toolUseId: toolId, title: String(event.tool_name), params: event.params, status: 'running' });
    } else if (type === 'tool.call_finished' || type === 'tool.call_failed') {
      const card = this.state.cards.findLast(item => item.kind === 'tool' && item.toolUseId === toolId);
      if (card) Object.assign(card, { status: type.endsWith('failed') ? 'failed' : 'success',
        output: String(event.output ?? event.error_message ?? ''), elapsedMs: Number(event.elapsed_ms ?? 0) });
    } else if (type === 'permission.requested') {
      Object.assign(this.add('permission', String(event.param_preview ?? ''), runId), {
        toolUseId: toolId, title: String(event.tool_name), params: event.params, status: 'pending'
      });
    } else if (type === 'permission.granted' || type === 'permission.denied') {
      const card = this.state.cards.findLast(item => item.kind === 'permission' && item.toolUseId === toolId);
      if (card) card.status = String(event.reason ?? event.decision ?? type);
    } else if (type === 'plan.updated') {
      const card = this.state.cards.findLast(item => item.kind === 'plan' && item.runId === runId) ?? this.add('plan', '', runId);
      card.text = String(event.explanation ?? '');
      card.plan = Array.isArray(event.plan) ? event.plan.map(item => ({ step: String(item.step), status: String(item.status) })) : [];
    } else if (type === 'llm.model_selected' && !this.children.has(runId)) {
      this.state.model = String(event.model ?? '');
    } else if (type === 'llm.usage' && !this.children.has(runId)) {
      this.state.usage = `输入 ${event.input_tokens ?? 0} · 输出 ${event.output_tokens ?? 0} · 缓存 ${event.cache_read_input_tokens ?? 0} · 上下文 ${Number(event.context_pct ?? 0).toFixed(1)}%`;
    } else if (type === 'subagent.finished') {
      const card = this.state.cards.findLast(item => item.kind === 'subagent' && item.runId === runId);
      if (card) card.status = String(event.status);
    } else if (type === 'run.finished') {
      this.add('notice', event.reason === 'cancelled' ? '已停止' : event.status === 'success' ? '已完成' : `运行失败：${event.reason ?? '未知原因'}`, runId);
      if (runId === this.state.runId) this.resolvePending(event.reason === 'cancelled' ? '已取消' : '已结束');
    } else if (type === 'session.waiting_for_input') {
      if (this.state.runId && event.last_run_id !== this.state.runId) return;
      this.state.busy = false; this.state.cancelling = false; this.state.runId = undefined;
      this.resolvePending('已结束'); this.streams.clear();
    } else if (type === 'session.closed') {
      this.fail('会话已关闭，请重试创建新会话'); return;
    } else if (type === 'log.line' && ['WARNING', 'ERROR'].includes(String(event.level))) {
      this.add('notice', `${event.level}: ${event.message ?? ''}`, runId);
    }
    this.publish();
  }

  // 新建有稳定标识的卡片供页面增量更新。
  private add(kind: Card['kind'], text: string, runId?: string): Card {
    const card: Card = { id: randomUUID(), kind, text, runId };
    this.state.cards.push(card); return card;
  }
  // 完成或中断时使尚未完成的工具与权限不可再操作。
  private resolvePending(status: string): void {
    for (const card of this.state.cards) {
      if (['running', 'pending', 'responding'].includes(card.status ?? '')) card.status = status;
    }
  }
  // 将高频 token 合并为约 50ms 一次的页面状态更新。
  private schedule(): void {
    if (!this.timer) this.timer = setTimeout(() => { this.timer = undefined; this.publish(); }, 50);
  }
  // 立即发送状态并合并尚未发出的流式刷新。
  private publish(): void {
    clearTimeout(this.timer); this.timer = undefined;
    if (!this.disposed) this.changed(this.state);
  }
  // 统一转为可展示的错误消息。
  private errorText(error: unknown): string { return error instanceof Error ? error.message : String(error); }
}
