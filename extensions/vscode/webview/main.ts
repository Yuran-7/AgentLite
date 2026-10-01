import type { Card, ChatState } from '../src/session';
import type { PageMessage } from '../src/protocol';
import { decisions } from '../src/protocol';
import { markdown } from './markdown';

declare function acquireVsCodeApi(): { postMessage(message: PageMessage): void };
const api = acquireVsCodeApi();
const cards = document.getElementById('cards')!;
const input = document.getElementById('input') as HTMLTextAreaElement;
const send = document.getElementById('send') as HTMLButtonElement;
const stop = document.getElementById('stop') as HTMLButtonElement;
const fresh = document.getElementById('new') as HTMLButtonElement;
const retry = document.getElementById('retry') as HTMLButtonElement;
const error = document.getElementById('error')!;
const rendered = new Map<string, { element: HTMLElement; signature: string }>();
const labels: Record<string, string> = { running: '运行中', pending: '等待确认', responding: '提交中',
  success: '成功', failed: '失败', allow_once: '允许一次', always_allow: '始终允许',
  deny_once: '拒绝一次', always_deny: '始终拒绝', auto_deny: '已自动拒绝', auto_allow: '已自动允许',
  timeout: '确认超时，已拒绝', run_cancelled: '任务已取消' };

// 将用户操作发送到扩展宿主，不在页面中访问 core。
function post(message: PageMessage): void { api.postMessage(message); }
// 在宿主可接受新任务时发送输入。
function submit(): void {
  if (send.disabled || !input.value.trim()) return;
  post({ type: 'send', content: input.value }); input.value = ''; send.disabled = true;
}
send.addEventListener('click', submit);
input.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit(); }
});
input.addEventListener('input', () => { send.disabled = input.disabled || !input.value.trim(); });
stop.addEventListener('click', () => post({ type: 'cancel' }));
fresh.addEventListener('click', () => post({ type: 'newSession' }));
retry.addEventListener('click', () => post({ type: 'retry' }));
document.getElementById('logs')!.addEventListener('click', () => post({ type: 'openLogs' }));

// 用 textContent 创建普通元素，避免工具输出变成 HTML。
function element(tag: string, text = '', className = ''): HTMLElement {
  const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
}

// 仅截断显示，保留完整工具结果以便用户展开。
function output(text: string, container: HTMLElement): void {
  const pre = element('pre', text.slice(0, 4000)); container.append(pre);
  if (text.length > 4000) {
    const button = element('button', `展开完整输出（${text.length.toLocaleString()} 字符）`);
    button.addEventListener('click', () => { pre.textContent = text; button.remove(); }); container.append(button);
  }
}

// 增量更新稳定卡片，并保留工具折叠状态与流式滚动位置。
function renderCard(card: Card, container: HTMLElement): void {
  const wasOpen = container.querySelector('details')?.open ?? false;
  container.replaceChildren();
  container.className = `card ${card.kind}`;
  if (card.kind === 'assistant') {
    container.append(element('div', 'AgentLite', 'role'));
    const body = element('div', '', 'markdown'); body.innerHTML = markdown(card.text); container.append(body);
  } else if (card.kind === 'user') {
    container.append(element('div', '你', 'role'), element('div', card.text, 'plain'));
  } else if (card.kind === 'tool') {
    const details = document.createElement('details'); details.open = wasOpen;
    details.append(element('summary', `${card.title} · ${labels[card.status ?? ''] ?? card.status ?? ''}${card.elapsedMs !== undefined ? ` · ${card.elapsedMs}ms` : ''}`));
    output(JSON.stringify(card.params ?? {}, null, 2), details);
    if (card.output !== undefined) output(card.output, details); container.append(details);
  } else if (card.kind === 'permission') {
    container.append(element('div', `权限确认 · ${card.title}`, 'role'));
    if (card.text) container.append(element('div', card.text, 'plain'));
    if (card.params) output(JSON.stringify(card.params, null, 2), container);
    container.append(element('div', labels[card.status ?? ''] ?? card.status ?? '', 'muted'));
    const actions = element('div', '', 'permission-actions');
    for (const decision of decisions) {
      const button = document.createElement('button'); button.textContent = labels[decision]; button.disabled = card.status !== 'pending';
      button.addEventListener('click', () => post({ type: 'permission', toolUseId: card.toolUseId!, decision })); actions.append(button);
    }
    container.append(actions);
  } else if (card.kind === 'plan') {
    container.append(element('div', '执行计划', 'role'), element('div', card.text));
    for (const item of card.plan ?? []) container.append(element('div', `${item.status === 'completed' ? '✓' : item.status === 'in_progress' ? '◉' : '○'} ${item.step}`, 'plan-step'));
  } else {
    container.append(element('div', card.kind === 'subagent' ? `子 Agent · ${card.text} · ${labels[card.status ?? ''] ?? card.status}` : card.text));
  }
  if (card.runId) container.dataset.runId = card.runId;
}

// 恢复完整宿主快照，普通状态更新只重绘发生变化的卡片。
function render(state: ChatState): void {
  document.getElementById('workspace')!.textContent = state.workspace;
  const origins = { reused: '复用已有 core', started: '本次启动共享 core' };
  const connection = { disconnected: '未连接', connecting: '连接中…', ready: '已连接', error: '连接中断' };
  const mode = state.coreMode === 'managed' ? ' · 随前端自动退出' : state.coreMode === 'persistent' ? ' · 手动常驻' : '';
  document.getElementById('connection')!.textContent = `${connection[state.connection]}${state.origin ? ` · ${origins[state.origin]}` : ''}${mode}${state.busy ? state.cancelling ? ' · 正在停止…' : ' · 运行中' : ''}`;
  document.getElementById('stats')!.textContent = [state.model, state.usage].filter(Boolean).join(' · ');
  error.hidden = !state.error; error.textContent = state.error ?? '';
  const locked = state.connection !== 'ready' || state.busy || state.sending;
  input.disabled = locked; send.disabled = locked || !input.value.trim();
  fresh.disabled = locked; retry.disabled = state.connection === 'connecting' || state.busy || state.sending;
  stop.disabled = !state.busy || !state.runId || state.cancelling;
  const atBottom = cards.scrollHeight - cards.scrollTop - cards.clientHeight < 80;
  const ids = new Set(state.cards.map(card => card.id));
  for (const [id, record] of rendered) if (!ids.has(id)) { record.element.remove(); rendered.delete(id); }
  if (!state.cards.length) {
    if (!cards.querySelector('.empty')) cards.append(element('div', '从一个任务开始\n读取文件、解释代码，或者执行一个修改。', 'empty'));
  } else cards.querySelector('.empty')?.remove();
  for (const card of state.cards) {
    let record = rendered.get(card.id);
    if (!record) { record = { element: element('article'), signature: '' }; rendered.set(card.id, record); cards.append(record.element); }
    const signature = JSON.stringify(card);
    if (signature !== record.signature) { renderCard(card, record.element); record.signature = signature; }
  }
  if (atBottom) cards.scrollTop = cards.scrollHeight;
}

window.addEventListener('message', event => {
  const message = event.data;
  if (message?.type === 'state') render(message.state as ChatState);
  else if (message?.type === 'unavailable') {
    error.hidden = false; error.textContent = message.message;
    input.disabled = true; send.disabled = true; fresh.disabled = true; stop.disabled = true; retry.disabled = false;
  }
});
post({ type: 'ready' });
