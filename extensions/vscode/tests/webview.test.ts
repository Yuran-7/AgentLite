import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import { webviewHtml } from '../src/html';
import type { ChatState } from '../src/session';
import { historyCards } from '../src/session';
import { codeBlocks } from '../src/code-blocks';
import { parsePageMessage } from '../src/protocol';

// 装载真实打包页面脚本，用宿主消息替身记录页面提交的操作。
function page() {
  const dom = new JSDOM(webviewHtml('nonce', 'local:', 'webview.js', 'chat.css'), { runScripts: 'outside-only' });
  const messages: any[] = [];
  Object.assign(dom.window, { acquireVsCodeApi: () => ({ postMessage: (message: unknown) => messages.push(message) }) });
  dom.window.eval(readFileSync('dist/webview.js', 'utf8'));
  return { dom, messages, document: dom.window.document,
    render: (state: ChatState) => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: { type: 'state', state } })) };
}
const state = (): ChatState => ({ workspace: 'C:/workspace', connection: 'ready', origin: 'reused',
  busy: false, sending: false, cancelling: false, model: 'demo', usage: '', cards: [] });

// 功能：上传预览可移除，图片消息在发送、编辑和历史恢复后保留附件。
// 设计：驱动真实页面的文件选择事件，覆盖异步读取及纯图片消息的发送边界。
test('image upload, remove, send, restore and edit preserve attachments', async t => {
  const fixture = page(); t.after(() => fixture.dom.window.close());
  const snapshot = state(); snapshot.sessionId = 'images'; fixture.render(snapshot);
  const { document, dom } = fixture;
  const picker = document.getElementById('image-picker') as HTMLInputElement;
  const data = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=';
  const file = new dom.window.File([Buffer.from(data, 'base64')], 'test.png', { type: 'image/png' });
  Object.defineProperty(picker, 'files', { configurable: true, value: [file] });
  picker.dispatchEvent(new dom.window.Event('change'));
  await new Promise(resolve => dom.window.setTimeout(resolve, 30));
  assert.equal(document.querySelectorAll('#image-attachments img').length, 1);
  (document.querySelector('#image-attachments button') as HTMLButtonElement).click();
  assert(document.getElementById('image-attachments')!.hidden);
  picker.dispatchEvent(new dom.window.Event('change'));
  await new Promise(resolve => dom.window.setTimeout(resolve, 30));
  (document.getElementById('send') as HTMLButtonElement).click();
  const message = parsePageMessage(fixture.messages.at(-1));
  assert(message?.type === 'send'); assert.equal(message.content, ''); assert.equal(message.images?.[0].data, data);
  assert(document.getElementById('image-attachments')!.hidden);
  snapshot.cards = historyCards([{ role: 'user', content: [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data } }] }]);
  fixture.render(snapshot); assert.equal(document.querySelectorAll('.card.user img').length, 1);
  (document.querySelector('.card.user [data-edit]') as HTMLButtonElement).click();
  assert.equal(document.querySelectorAll('#image-attachments img').length, 1);
  assert(!(document.getElementById('send') as HTMLButtonElement).disabled);
  assert.equal(parsePageMessage({ type: 'send', content: '', images: [{ name: 'bad', media_type: 'image/svg+xml', data }] }), undefined);
});

test('MCP slash command opens live status, expands tools safely and uses validated actions', t => {
  const fixture = page(); t.after(() => fixture.dom.window.close()); const snapshot = state();
  snapshot.mcpServers = [{ name: 'filesystem', transport: 'stdio', enabled: true, status: 'connected', command: 'python',
    tools: [{ name: 'filesystem__read', description: '<img src=x onerror=alert(1)>', inputSchema: { type: 'object', properties: { path: { type: 'string' } } } }] },
    { name: 'remote', transport: 'http', enabled: true, status: 'error', url: 'https://example.test/mcp', error: '需要身份验证', tools: [] },
    { name: 'disabled', transport: 'tcp', enabled: false, status: 'disabled', tools: [] }];
  fixture.render(snapshot);
  const { document, dom } = fixture; const input = document.getElementById('input') as HTMLTextAreaElement;
  input.value = '/mcp'; input.dispatchEvent(new dom.window.Event('input'));
  assert(!document.getElementById('slash-mcp')!.textContent?.includes('待实现'));
  input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  const panel = document.getElementById('mcp-panel')!;
  assert(!panel.hidden); assert.equal(fixture.messages.at(-1).type, 'refreshMcp'); assert.equal(input.value, '');
  assert.equal(document.getElementById('mcp-summary')!.textContent, '1 / 3 已连接 · 1 个工具');
  const expand = document.querySelector('.mcp-expand') as HTMLButtonElement; expand.click();
  assert.equal(expand.getAttribute('aria-expanded'), 'true');
  assert.equal(document.querySelector('.mcp-tool p')!.textContent, '<img src=x onerror=alert(1)>');
  assert.equal(panel.querySelector('img'), null);
  (document.querySelector('.mcp-tool') as HTMLDetailsElement).open = true;
  snapshot.usage = '输入 10'; fixture.render(snapshot);
  assert((document.querySelector('.mcp-tool') as HTMLDetailsElement).open);
  assert.equal(expand.getAttribute('aria-expanded'), 'true');
  (document.querySelector('.mcp-toggle') as HTMLButtonElement).click();
  assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'setMcpEnabled', name: 'filesystem', enabled: false });
  (document.querySelector('.mcp-reconnect') as HTMLButtonElement).click();
  assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'reconnectMcp', name: 'filesystem' });
  document.getElementById('mcp-add')!.click(); assert.equal(fixture.messages.at(-1).type, 'addMcpServer');
  document.getElementById('mcp-configure')!.click(); assert.equal(fixture.messages.at(-1).type, 'openMcpSettings');
  document.getElementById('mcp-apply')!.click(); assert.equal(fixture.messages.at(-1).type, 'reloadMcp');
  assert(!fixture.messages.some(message => message.type === 'send'));
  document.getElementById('mcp-close')!.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert(panel.hidden); assert.equal(document.activeElement, input);
});

test('MCP panel shows empty and old-core errors, locks mutations during tasks and closes outside', t => {
  const fixture = page(); t.after(() => fixture.dom.window.close()); const snapshot = state(); snapshot.mcpServers = []; fixture.render(snapshot);
  const { document, dom } = fixture; const input = document.getElementById('input') as HTMLTextAreaElement;
  input.value = '/mcp'; input.dispatchEvent(new dom.window.Event('input')); document.getElementById('slash-mcp')!.click();
  const panel = document.getElementById('mcp-panel')!;
  assert(!panel.hidden); assert(panel.textContent?.includes('尚未配置'));
  snapshot.busy = true; fixture.render(snapshot);
  assert((document.getElementById('mcp-add') as HTMLButtonElement).disabled);
  assert((document.getElementById('mcp-apply') as HTMLButtonElement).disabled);
  assert(!(document.getElementById('mcp-refresh') as HTMLButtonElement).disabled);
  assert(!(document.getElementById('mcp-configure') as HTMLButtonElement).disabled);
  snapshot.busy = false; snapshot.mcpBusy = true; fixture.render(snapshot);
  assert(input.disabled); assert((document.getElementById('mcp-refresh') as HTMLButtonElement).disabled);
  snapshot.mcpBusy = false; snapshot.mcpError = '当前 core 尚不支持 MCP 管理，请重启 core'; fixture.render(snapshot);
  assert(document.getElementById('mcp-message')!.textContent?.includes('重启 core'));
  document.getElementById('cards')!.click(); assert(panel.hidden);
  assert.equal(parsePageMessage({ type: 'setMcpEnabled', name: 'server', enabled: 'false' }), undefined);
  assert.equal(parsePageMessage({ type: 'reconnectMcp', name: '' }), undefined);
  assert.equal(parsePageMessage({ type: 'mcp.manage', action: 'add', server: { command: 'arbitrary' } }), undefined);
});

test('slash menu filters, navigates and dispatches existing commands without sending prompts', t => {
  const fixture = page(); t.after(() => fixture.dom.window.close()); fixture.render(state());
  const { document, dom } = fixture;
  const input = document.getElementById('input') as HTMLTextAreaElement;
  const type = (value: string) => { input.value = value; input.dispatchEvent(new dom.window.Event('input')); };
  const key = (value: string) => input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true }));
  type('/'); assert(!document.getElementById('slash-menu')!.hidden);
  assert.equal(document.querySelectorAll('#slash-options [role="option"]').length, 12);
  assert.equal(input.getAttribute('aria-activedescendant'), 'slash-new');
  key('ArrowUp'); assert.equal(input.getAttribute('aria-activedescendant'), 'slash-skills');
  key('ArrowDown'); key('Enter');
  assert.equal(fixture.messages.at(-1).type, 'newSession'); assert.equal(input.value, '');
  type('/模型'); assert.equal(document.querySelectorAll('#slash-options [role="option"]').length, 4);
  type('/settings'); key('Tab'); assert.equal(fixture.messages.at(-1).type, 'openSettings');
  type('/logs'); document.getElementById('send')!.click(); assert.equal(fixture.messages.at(-1).type, 'openLogs');
  type('/model'); document.getElementById('slash-model')!.click(); assert(!document.getElementById('model-menu')!.hidden);
  type('/history'); key('Enter'); assert(!document.getElementById('history-panel')!.hidden);
  assert.equal(document.activeElement?.id, 'history-search');
  assert(!fixture.messages.some(message => message.type === 'send'));
});

test('slash placeholders and status have dismissible details and never create model tasks', t => {
  const fixture = page(); t.after(() => fixture.dom.window.close());
  const snapshot = state(); snapshot.sessionId = 'session-123'; snapshot.usage = '输入 100 · 输出 30'; fixture.render(snapshot);
  const { document, dom } = fixture; const input = document.getElementById('input') as HTMLTextAreaElement;
  const type = (value: string) => { input.value = value; input.dispatchEvent(new dom.window.Event('input')); };
  type('/goal'); document.getElementById('slash-goal')!.click();
  const detail = document.getElementById('command-detail')!;
  assert(!detail.hidden); assert(document.getElementById('command-body')!.textContent?.includes('尚未实现'));
  assert.equal(input.value, ''); assert.equal(document.activeElement?.id, 'command-close');
  document.getElementById('command-close')!.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert(detail.hidden); assert.equal(document.activeElement, input);
  type('/status'); document.getElementById('send')!.click(); assert(!detail.hidden);
  assert(detail.textContent?.includes('session-123')); assert(detail.textContent?.includes(snapshot.usage));
  assert(!fixture.messages.some(message => message.type === 'send'));
  document.getElementById('cards')!.click(); assert(detail.hidden);
});

test('slash dismissal, ordinary text, composition and busy state preserve composer behavior', t => {
  const fixture = page(); t.after(() => fixture.dom.window.close()); const snapshot = state(); fixture.render(snapshot);
  const { document, dom } = fixture; const input = document.getElementById('input') as HTMLTextAreaElement;
  const menu = document.getElementById('slash-menu')!;
  const type = (value: string) => { input.value = value; input.dispatchEvent(new dom.window.Event('input')); };
  const key = (value: string, extra = {}) => input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true, ...extra }));
  type('/'); input.click(); assert(!menu.hidden);
  key('Escape'); assert(menu.hidden); assert.equal(input.value, '/');
  type('/'); document.getElementById('cards')!.click(); assert(menu.hidden);
  type('/'); input.dispatchEvent(new dom.window.CompositionEvent('compositionstart')); assert(menu.hidden);
  key('Enter', { isComposing: true }); assert.equal(fixture.messages.length, 1);
  input.dispatchEvent(new dom.window.CompositionEvent('compositionend')); assert(!menu.hidden);
  type('/no-such-command'); assert(document.getElementById('slash-options')!.textContent?.includes('没有匹配'));
  type('解释 /tmp/example'); assert(menu.hidden); key('Enter', { shiftKey: true }); assert.equal(fixture.messages.length, 1);
  key('Enter'); assert.equal(fixture.messages.at(-1).content, '解释 /tmp/example');
  type('/'); snapshot.busy = true; fixture.render(snapshot); assert(menu.hidden);
  key('Enter'); assert.equal(fixture.messages.filter(message => message.type === 'send').length, 1);
});

// 功能：Thinking 随用户消息一起滚动，历史面板可从外部关闭且条目操作没有悬停介绍。
// 设计：验证真实页面节点归属及内外点击，覆盖刷新、改名、归档和 Escape 的交互边界。
test('thinking follows prompt and history dismisses outside without row tooltips', t => {
  const fixture = page(); t.after(() => fixture.dom.window.close());
  const snapshot = state(); snapshot.cards = [{ id: 'u', kind: 'user', text: 'question' }];
  snapshot.busy = true; snapshot.runId = 'run'; fixture.render(snapshot);
  const { document, dom } = fixture;
  const thinking = document.getElementById('run-status')!;
  assert.equal(thinking.parentElement?.id, 'cards');
  assert.equal(thinking.previousElementSibling?.className, 'card user');
  assert(!thinking.hidden); assert.equal(document.querySelector('footer #run-status'), null);
  snapshot.busy = false; snapshot.runId = undefined;
  snapshot.history = [{ session_id: 'old', title: 'Previous conversation', updated_at: '2026-10-02T00:00:00Z' }];
  fixture.render(snapshot);
  const toggle = document.getElementById('history-toggle')!;
  const panel = document.getElementById('history-panel')!;
  toggle.click(); assert(!panel.hidden);
  assert.equal(document.querySelectorAll('#history-list [data-tooltip], #history-list [title]').length, 0);
  assert(document.querySelector('.history-rename')?.getAttribute('aria-label'));
  document.getElementById('history-search')!.click(); assert(!panel.hidden);
  (document.querySelector('.history-rename') as HTMLButtonElement).click(); assert(!panel.hidden);
  (document.querySelector('.history-archive') as HTMLButtonElement).click(); assert(!panel.hidden);
  document.getElementById('history-refresh')!.click(); assert(!panel.hidden);
  document.getElementById('cards')!.click(); assert(panel.hidden);
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  toggle.click(); assert(!panel.hidden);
  document.getElementById('input')!.click(); assert(panel.hidden);
  toggle.click(); document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert(panel.hidden); assert.equal(document.activeElement, toggle);
});

// 功能：统计位于输入框下方，运行从 Thinking 切换到聊天区计时，全程只显示一个状态。
// 设计：控制时钟并依次渲染首次内容、取消和终态，覆盖完成事件与空闲事件之间的窗口。
test('composer usage replaces hint and run status has a single location', t => {
  const fixture = page(); t.after(() => fixture.dom.window.close());
  const snapshot = state(); snapshot.usage = '输入 100 · 输出 30 · 缓存 20 · 上下文 1.3%';
  fixture.render(snapshot);
  const { document, dom } = fixture;
  const stats = document.getElementById('stats')!;
  assert.equal(stats.previousElementSibling?.className, 'composer-shell');
  assert.equal(stats.textContent, snapshot.usage);
  assert.equal(document.querySelector('.composer-hint'), null);
  assert(!document.body.textContent?.includes('Enter 发送'));
  snapshot.busy = true; snapshot.runId = 'run'; snapshot.workStartedAt = 100_000;
  dom.window.Date.now = () => 114_000;
  fixture.render(snapshot);
  const status = document.getElementById('run-status')!;
  assert(!status.hidden); assert.equal(document.getElementById('run-label')!.textContent, 'Thinking…');
  snapshot.cards.push({ id: 'answer', kind: 'assistant', text: 'checking', runId: 'run' });
  fixture.render(snapshot);
  assert(status.hidden);
  assert.equal(document.querySelector('.work-progress')?.textContent, 'Working for 14s');
  assert.equal(document.querySelector('.work-summary'), null);
  snapshot.cancelling = true; fixture.render(snapshot);
  assert(status.hidden); assert.equal(document.querySelector('.work-progress')?.textContent, 'Stopping…');
  snapshot.cards[0].completed = true; snapshot.cards[0].workMs = 15_000;
  fixture.render(snapshot);
  assert(status.hidden); assert.equal(document.querySelector('.work-summary summary')?.textContent, 'Worked for 15s');
  snapshot.busy = false; snapshot.runId = undefined; snapshot.cancelling = false;
  fixture.render(snapshot); assert(status.hidden);
});

// 功能：代码块显示语言和高亮，纯文本保持原样，复制及打开按索引取源代码且不执行内容。
// 设计：混合 Python、PowerShell、AST 和恶意 HTML，经过真实 Markdown 与宿主取码路径验证。
test('code blocks highlight safely and actions address exact source blocks', t => {
  const fixture = page(); t.after(() => fixture.dom.window.close());
  const snapshot = state();
  const text = '```python\nbuilder.add_node("agent", model_node)\n```\n\n```powershell\nbun run dev --resume "abc"\n```\n\n```text\nReturn\n└── Call\n    ├── func\n```\n\n```unknown\n<img src=x onerror=alert(1)>\n```';
  snapshot.cards = [{ id: 'code-answer', kind: 'assistant', text }]; fixture.render(snapshot);
  const blocks = fixture.document.querySelectorAll('.code-block');
  assert.equal(blocks.length, 4);
  assert.equal(blocks[0].querySelector('.code-language')?.textContent, '‹/›Python');
  assert.equal(blocks[0].querySelector('.hljs-string')?.textContent, '"agent"');
  assert(blocks[1].querySelector('.hljs-string'));
  assert.equal(blocks[2].querySelector('pre code')?.textContent, 'Return\n└── Call\n    ├── func\n');
  assert.equal(blocks[3].querySelector('img'), null);
  assert.equal(blocks[3].querySelector('pre code')?.textContent?.trim(), '<img src=x onerror=alert(1)>');
  (blocks[1].querySelector('[aria-label="复制代码"]') as HTMLButtonElement).click();
  assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'copyCode', cardId: 'code-answer', blockIndex: 1 });
  assert.equal(codeBlocks(text)[1].text, 'bun run dev --resume "abc"');
  (blocks[0].querySelector('[aria-label="在编辑器中打开代码"]') as HTMLButtonElement).click();
  assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'openCode', cardId: 'code-answer', blockIndex: 0 });
  assert.equal(parsePageMessage({ type: 'copyCode', cardId: 'x', blockIndex: -1 }), undefined);
  assert.equal(parsePageMessage({ type: 'copyCode', cardId: 'x', blockIndex: 1.2 }), undefined);
  assert.equal(codeBlocks('> ```js\n> console.log(1)\n> ```')[0].text, 'console.log(1)');
});

// 功能：流式文字和权限等待不显示完成图标，整轮结束后所有文字都排在唯一耗时标题后。
// 设计：重放多段输出、工具与权限交错的真实快照，并验证下一轮运行不会改变上一轮展示。
test('answer completion and work summary belong to the whole run', t => {
  const fixture = page(); const snapshot = state();
  t.after(() => fixture.dom.window.close());
  snapshot.busy = true; snapshot.runId = 'run'; snapshot.sessionId = 'session';
  snapshot.cards = [
    { id: 'u', kind: 'user', text: 'question' },
    { id: 'a1', kind: 'assistant', text: '先检查目录', runId: 'run' },
    { id: 'p', kind: 'permission', text: 'shell', status: 'pending', runId: 'run' },
    { id: 't', kind: 'tool', text: '', title: 'shell', status: 'running', runId: 'run' }
  ];
  fixture.render(snapshot);
  assert.equal(fixture.document.querySelector('.answer-actions'), null);
  assert.equal(fixture.document.querySelector('.work-progress')?.textContent, 'Working…');
  assert.equal(fixture.document.querySelector('.work-summary'), null);
  assert.equal(fixture.document.querySelector('#cards > .assistant > .markdown')?.textContent?.trim(), '先检查目录');
  snapshot.cards.push({ id: 'a2', kind: 'assistant', text: '当前目录是…', runId: 'run' });
  fixture.render(snapshot);
  assert.equal(fixture.document.querySelector('.answer-actions'), null);
  const streaming = [...fixture.document.querySelectorAll<HTMLElement>('#cards > article')];
  assert(streaming.every(node => !node.hidden));
  assert.equal(streaming.at(-1)?.querySelector('.markdown')?.textContent?.trim(), '当前目录是…');
  assert.equal(fixture.document.querySelectorAll('.work-progress').length, 1);
  snapshot.cards[2].status = 'denied'; snapshot.cards[3].status = 'success';
  snapshot.cards[4].workMs = 186_000;
  const originalIds = snapshot.cards.map(card => card.id);
  fixture.render(snapshot);
  assert.equal(fixture.document.querySelectorAll('.work-summary').length, 1);
  assert.equal(fixture.document.querySelector('.work-summary summary')?.textContent, 'Worked for 3m 6s');
  const articles = [...fixture.document.querySelectorAll('#cards > article')];
  assert.deepEqual(articles.map(node => node.className), ['card user', 'card permission', 'card tool', 'card assistant', 'card assistant']);
  assert((articles[3] as HTMLElement).hidden);
  assert(articles[4].querySelector('.work-summary'));
  assert.equal((articles[4].querySelector('.work-summary') as HTMLDetailsElement).open, false);
  assert.equal(fixture.document.querySelector('.work-progress'), null);
  assert.equal(articles[4].lastElementChild?.className, 'answer-actions');
  assert.equal(articles[4].querySelector('.work-activity .assistant .markdown')?.textContent?.trim(), '先检查目录');
  assert.equal(articles[4].querySelector(':scope > .markdown')?.textContent?.trim(), '当前目录是…');
  assert.deepEqual(snapshot.cards.map(card => card.id), originalIds);
  snapshot.busy = false; snapshot.runId = undefined; fixture.render(snapshot);
  const summary = fixture.document.querySelector('.work-summary') as HTMLDetailsElement;
  summary.open = true;
  snapshot.busy = true; snapshot.runId = 'next';
  snapshot.cards.push({ id: 'u2', kind: 'user', text: 'next question' },
    { id: 'a3', kind: 'assistant', text: '继续检查', runId: 'next' });
  fixture.render(snapshot);
  assert((fixture.document.querySelector('.work-summary') as HTMLDetailsElement).open);
  assert.equal(fixture.document.querySelectorAll('.answer-actions').length, 1);
  assert.equal(fixture.document.querySelector('#cards > article:last-child .answer-actions'), null);
  (fixture.document.querySelector('#cards > .assistant [data-copy]') as HTMLButtonElement).click();
  assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'copyAnswer', cardId: 'a2' });
  fixture.dom.window.close();
});

// 功能：恢复的多轮历史只在分割线下面显示最终回答，中间文字和工具输出默认折叠。
// 设计：经过真实历史转换函数，覆盖旧 core 缺少运行信息和新 core 提供精确耗时两种情况。
test('restored history groups commentary separately from final answers', t => {
  const fixture = page(); t.after(() => fixture.dom.window.close());
  const snapshot = state();
  snapshot.cards = historyCards([
    { role: 'user', content: 'question' },
    { role: 'assistant', content: [{ type: 'text', text: 'checking' }, { type: 'tool_use', id: 'tool', name: 'shell', input: {} }] },
    { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tool', content: 'result' }] },
    { role: 'assistant', content: 'final' },
    { role: 'user', run_id: 'next', content: 'question2' },
    { role: 'assistant', run_id: 'next', work_ms: 5000, content: 'final2' }
  ]);
  fixture.render(snapshot);
  const summaries = [...fixture.document.querySelectorAll('.work-summary')];
  assert.equal(summaries.length, 2);
  assert.equal(summaries[0].querySelector('summary')?.textContent, 'Worked for · 耗时未记录');
  assert.equal(summaries[1].querySelector('summary')?.textContent, 'Worked for 5s');
  assert(summaries.every(node => !(node as HTMLDetailsElement).open));
  assert.equal(summaries[0].querySelector('.work-activity .markdown')?.textContent?.trim(), 'checking');
  const visible = [...fixture.document.querySelectorAll<HTMLElement>('#cards > .assistant')].filter(node => !node.hidden);
  assert.deepEqual(visible.map(node => node.querySelector(':scope > .markdown')?.textContent?.trim()), ['final', 'final2']);
  assert.equal(fixture.document.querySelectorAll('.answer-actions').length, 2);
});

// 功能：所有静态和动态悬停文字使用统一气泡，支持键盘、状态更新和边界避让。
// 设计：用真实页面委托事件和模拟布局检查交互，覆盖 SVG 子节点、禁用按钮与原生 title 迁移。
test('shared tooltips cover all controls and dynamic titles safely', async () => {
  const fixture = page(); fixture.render(state());
  const { document, dom } = fixture;
  const tooltip = document.getElementById('app-tooltip')!;
  Object.defineProperty(dom.window, 'innerWidth', { value: 220 });
  Object.defineProperty(dom.window, 'innerHeight', { value: 100 });
  tooltip.getBoundingClientRect = () => ({ width: 180, height: 28 } as DOMRect);
  // 模拟布局并从按钮的 SVG 子元素触发鼠标事件。
  function hover(target: HTMLElement): void {
    target.getClientRects = () => [{ width: 30, height: 30 }] as unknown as DOMRectList;
    target.getBoundingClientRect = () => ({ left: 190, top: 80, bottom: 110, width: 30, height: 30 } as DOMRect);
    (target.querySelector('svg') || target).dispatchEvent(new dom.window.MouseEvent('mouseover', { bubbles: true }));
  }
  assert.equal(document.querySelectorAll('[title]').length, 0);
  assert.equal(document.getElementById('attach')!.dataset.tooltip, '添加上下文');
  assert.equal(document.getElementById('attachment-tooltip'), null);
  for (const id of ['attach', 'settings', 'connection-toggle', 'model-toggle', 'session-title',
    'bookmark', 'history-toggle', 'new', 'history-refresh', 'send', 'bookmarks-close']) {
    const target = document.getElementById(id)!;
    if (target.closest('[hidden]') || target.hidden) continue;
    hover(target);
    assert.equal(tooltip.textContent, target.dataset.tooltip);
    assert(!tooltip.hidden); assert(target.getAttribute('aria-describedby')?.includes('app-tooltip'));
    assert.equal(tooltip.style.left, '32px'); assert.equal(tooltip.style.top, '46px');
    document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    assert(tooltip.hidden); assert(!target.hasAttribute('aria-describedby'));
  }
  const dynamic = document.createElement('button'); dynamic.title = '<img src=x> 动态提示';
  document.body.append(dynamic); hover(dynamic);
  assert.equal(dynamic.title, ''); assert.equal(tooltip.textContent, '<img src=x> 动态提示');
  assert.equal(tooltip.querySelector('img'), null);
  dynamic.dataset.tooltip = '已复制';
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(tooltip.textContent, '已复制');
  document.dispatchEvent(new dom.window.Event('scroll')); assert(tooltip.hidden);
  dynamic.dispatchEvent(new dom.window.FocusEvent('focusin', { bubbles: true })); assert(!tooltip.hidden);
  dynamic.dispatchEvent(new dom.window.FocusEvent('focusout', { bubbles: true })); assert(tooltip.hidden);
  hover(dynamic); dynamic.remove();
  await new Promise(resolve => setTimeout(resolve, 0)); assert(tooltip.hidden);
  const snapshot = state(); snapshot.cards = [{ id: 'answer', kind: 'assistant', text: 'answer' }];
  fixture.render(snapshot);
  const copy = document.querySelector<HTMLElement>('[data-copy]')!; hover(copy);
  assert.equal(tooltip.textContent, '复制回答');
  document.body.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })); assert(tooltip.hidden);
  assert.equal(document.querySelectorAll('[title]').length, 0);
  dom.window.close();
});

// 功能：连接中的秒数自动增加，成功和失败后固定，重建页面保留实际连接耗时。
// 设计：控制页面时钟并等待真实刷新计时器，确保不用宿主心跳也能更新显示。
test('connection elapsed seconds tick and freeze across snapshots and page recreation', async () => {
  const fixture = page(); const snapshot = state();
  let now = 100_000; fixture.dom.window.Date.now = () => now;
  snapshot.connection = 'connecting'; snapshot.connectionStartedAt = now;
  fixture.render(snapshot);
  const text = () => fixture.document.getElementById('connection')!.textContent;
  assert.equal(text(), '正在连接 core · 已等待 0.0 秒');
  now += 12_300;
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.equal(text(), '正在连接 core · 已等待 12.3 秒');
  snapshot.connection = 'ready'; snapshot.connectionElapsedMs = 12_450; fixture.render(snapshot);
  assert.equal(text(), '连接 core 花了 12.4 秒');
  assert.equal(fixture.document.getElementById('connection-toggle')!.dataset.tooltip, '连接信息 · 连接 core 花了 12.4 秒');
  now += 30_000;
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.equal(text(), '连接 core 花了 12.4 秒');
  const recreated = page(); recreated.render(snapshot);
  assert.equal(recreated.document.getElementById('connection')!.textContent, text());
  recreated.dom.window.close();
  snapshot.connection = 'connecting'; snapshot.connectionStartedAt = now;
  snapshot.connectionElapsedMs = undefined; fixture.render(snapshot);
  assert.equal(text(), '正在连接 core · 已等待 0.0 秒');
  snapshot.connection = 'error'; snapshot.connectionElapsedMs = 4500; fixture.render(snapshot);
  now += 30_000;
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.equal(text(), '连接中断 · 连接耗时 4.5 秒');
  fixture.dom.window.close();
});

// 功能：连接图标展示实时信息，运行中保持打开，支持日志、重连和 Escape 关闭。
// 设计：使用真实模板检查旧入口移除，跨忙碌快照更新与页面操作消息保持可用。
test('connection popover replaces model refresh and remains usable while running', () => {
  const fixture = page(); const snapshot = state(); fixture.render(snapshot);
  assert.equal(fixture.document.getElementById('model-refresh'), null);
  assert.equal(fixture.document.querySelector('.connection-details'), null);
  assert.equal(fixture.document.querySelector('.empty-logo')?.getAttribute('draggable'), 'false');
  const toggle = fixture.document.getElementById('connection-toggle') as HTMLButtonElement;
  toggle.click(); assert(!fixture.document.getElementById('connection-menu')!.hidden);
  assert.equal(fixture.document.getElementById('workspace')?.textContent, snapshot.workspace);
  const counts = fixture.document.getElementById('connection-counts')!;
  assert.equal(counts.textContent, '连接数量暂不可用');
  snapshot.frontendCounts = { vscode: 2, tui: 1 }; fixture.render(snapshot);
  assert.equal(counts.textContent, 'VS Code 插件：2 · TUI：1');
  assert(!counts.hidden);
  snapshot.busy = true; fixture.render(snapshot);
  snapshot.frontendCounts = { vscode: 1, tui: 0 }; fixture.render(snapshot);
  assert.equal(counts.textContent, 'VS Code 插件：1 · TUI：0');
  assert(!fixture.document.getElementById('connection-menu')!.hidden);
  assert.equal(fixture.document.getElementById('connection')!.textContent, 'core 已连接');
  fixture.document.getElementById('logs')!.click(); assert.equal(fixture.messages.at(-1).type, 'openLogs');
  fixture.document.dispatchEvent(new fixture.dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert(fixture.document.getElementById('connection-menu')!.hidden); assert.equal(fixture.document.activeElement, toggle);
  snapshot.busy = false; fixture.render(snapshot); toggle.click();
  fixture.document.getElementById('retry')!.click(); assert.equal(fixture.messages.at(-1).type, 'retry');
  snapshot.connection = 'error'; fixture.render(snapshot); assert(counts.hidden);
  fixture.dom.window.close();
});

// 功能：耗时分隔栏收起工具过程，用户消息可复制与回填，忙碌时禁止编辑。
// 设计：使用真实页面和快照验证操作及折叠状态保留，而不是只断言样式字符串。
test('worked summary, message hover actions and editing preserve chat behavior', () => {
  const fixture = page(); const snapshot = state();
  snapshot.cards = [{ id: 'u', kind: 'user', text: 'my question', createdAt: '2026-10-02T00:00:00Z' },
    { id: 't', kind: 'tool', title: 'read_file', runId: 'run', text: '', status: 'success', output: 'result' },
    { id: 'a', kind: 'assistant', text: '**done**', runId: 'run', workMs: 261_000 }];
  fixture.render(snapshot);
  assert.equal(fixture.document.querySelector('.work-summary summary')?.textContent, 'Worked for 4m 21s');
  assert.equal(fixture.document.querySelector('.work-activity .tool pre:last-child')?.textContent, 'result');
  assert((fixture.document.querySelector('#cards > .tool') as HTMLElement).hidden);
  const work = fixture.document.querySelector('.work-summary') as HTMLDetailsElement; work.open = true;
  snapshot.cards[2].text += '!'; fixture.render(snapshot);
  assert((fixture.document.querySelector('.work-summary') as HTMLDetailsElement).open);
  (fixture.document.querySelector('.user [data-copy]') as HTMLButtonElement).click();
  assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'copyMessage', cardId: 'u' });
  (fixture.document.querySelector('.user [data-edit]') as HTMLButtonElement).click();
  assert.equal((fixture.document.getElementById('input') as HTMLTextAreaElement).value, 'my question');
  assert.equal(fixture.document.querySelector('time')?.getAttribute('datetime'), '2026-10-02T00:00:00Z');
  snapshot.busy = true; fixture.render(snapshot);
  assert((fixture.document.querySelector('.user [data-edit]') as HTMLButtonElement).disabled);
  assert(!fixture.document.getElementById('run-status')!.hidden);
  fixture.dom.window.close();
});

// 功能：输入框增高后可收缩，自定义模型菜单支持键盘和安全切换。
// 设计：模拟浏览器测量高度并分发实际键盘事件，检查发送后收缩及忙碌关闭菜单。
test('auto growing composer and keyboard accessible model menu', () => {
  const fixture = page(); const snapshot = state();
  snapshot.models = [{ id: 'a', name: 'Model A', model: 'model-a', protocol: 'openai' },
    { id: 'b', name: 'Model B', model: 'model-b', protocol: 'anthropic' }];
  snapshot.selectedModel = 'a'; fixture.render(snapshot);
  const input = fixture.document.getElementById('input') as HTMLTextAreaElement;
  let height = 110; Object.defineProperty(input, 'scrollHeight', { get: () => height });
  input.value = 'multiple\nlines'; input.dispatchEvent(new fixture.dom.window.Event('input'));
  assert.equal(input.style.height, '110px');
  height = 400; input.dispatchEvent(new fixture.dom.window.Event('input'));
  assert.equal(input.style.height, '180px'); assert.equal(input.style.overflowY, 'auto');
  height = 42; input.dispatchEvent(new fixture.dom.window.KeyboardEvent('keydown', { key: 'Enter', cancelable: true }));
  assert.equal(input.value, ''); assert.equal(input.style.height, '42px');
  const toggle = fixture.document.getElementById('model-toggle') as HTMLButtonElement;
  toggle.dispatchEvent(new fixture.dom.window.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
  assert.equal(fixture.document.activeElement?.getAttribute('aria-selected'), 'true');
  fixture.document.getElementById('model-menu')!.dispatchEvent(new fixture.dom.window.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
  assert.equal(fixture.document.activeElement?.textContent, 'Model Banthropic · model-b');
  (fixture.document.activeElement as HTMLButtonElement).click();
  assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'selectModel', modelId: 'b' });
  assert(fixture.document.getElementById('model-menu')!.hidden);
  snapshot.busy = true; fixture.render(snapshot); assert(toggle.disabled);
  fixture.dom.window.close();
});

// 功能：回答操作提交消息标识，用户消息和空回答不显示回答工具栏。
// 设计：使用打包页面验证实际点击消息，并检查宿主解析器拒绝缺失或非字符串标识。
test('answer actions use validated card IDs without accepting page-provided content', () => {
  const fixture = page(); const snapshot = state();
  snapshot.cards = [{ id: 'user', kind: 'user', text: 'hello' },
    { id: 'answer', kind: 'assistant', text: '你好！' }, { id: 'empty', kind: 'assistant', text: '' }];
  fixture.render(snapshot);
  assert.equal(fixture.document.querySelector('.user .answer-actions'), null);
  assert.equal(fixture.document.querySelectorAll('.answer-actions').length, 1);
  const buttons = fixture.document.querySelectorAll<HTMLButtonElement>('.answer-actions button');
  buttons[0].click(); assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'copyAnswer', cardId: 'answer' });
  buttons[1].click(); assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'openAnswer', cardId: 'answer' });
  assert.equal(parsePageMessage({ type: 'copyAnswer', content: 'untrusted' }), undefined);
  assert.equal(parsePageMessage({ type: 'openAnswer', cardId: 123 }), undefined);
  assert.equal(parsePageMessage({ type: 'copyAnswer', cardId: '' }), undefined);
  fixture.dom.window.close();
});

test('session header renames chats, opens bookmarks and history, and creates a chat', () => {
  const fixture = page(); const snapshot = state();
  snapshot.sessionId = 'current'; snapshot.title = '<img src=x> My session'; fixture.render(snapshot);
  const header = fixture.document.querySelector('header')!;
  const title = fixture.document.getElementById('session-title') as HTMLButtonElement;
  assert.equal(title.textContent, snapshot.title); assert.equal(title.querySelector('img'), null);
  assert.deepEqual([...header.querySelectorAll('button')].map(button => button.id), ['session-title', 'bookmark', 'history-toggle', 'new']);
  title.click(); assert.deepEqual(JSON.parse(JSON.stringify(fixture.messages.at(-1))), { type: 'renameSession', sessionId: 'current' });
  const count = fixture.messages.length; fixture.document.getElementById('bookmark')!.click();
  assert.equal(fixture.messages.length, count);
  assert(!fixture.document.getElementById('bookmarks-panel')!.hidden);
  fixture.document.getElementById('history-toggle')!.click(); assert.equal(fixture.messages.at(-1).type, 'refreshHistory');
  fixture.document.getElementById('new')!.click(); assert.equal(fixture.messages.at(-1).type, 'newSession');
  snapshot.busy = true; fixture.render(snapshot); assert(title.disabled);
  snapshot.busy = false; snapshot.sessionId = undefined; fixture.render(snapshot); assert(title.disabled);
  fixture.dom.window.close();
});

// 功能：回答收藏、面板预览和取消操作使用可信标识，旧原文缺失仍可阅读保存内容。
// 设计：模拟宿主保存后的快照，检查选中状态、安全 Markdown、回跳及忙碌禁用。
test('response bookmarks preview safely and use validated actions', () => {
  const fixture = page(); const snapshot = state(); snapshot.sessionId = 'session';
  snapshot.cards = [{ id: 'answer', kind: 'assistant', text: '**saved** <img src=x>' }]; fixture.render(snapshot);
  (fixture.document.querySelector('[data-bookmark]') as HTMLButtonElement).click();
  assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'toggleBookmark', cardId: 'answer' });
  fixture.dom.window.dispatchEvent(new fixture.dom.window.MessageEvent('message', { data: { type: 'bookmarksSettled' } }));
  assert(!(fixture.document.querySelector('[data-bookmark]') as HTMLButtonElement).disabled);
  snapshot.bookmarks = [{ id: 'saved', sessionId: 'session', sourceKey: 'key', text: snapshot.cards[0].text, savedAt: '2026-10-02T00:00:00Z' }];
  snapshot.bookmarkCardIds = { answer: 'saved' }; fixture.render(snapshot);
  assert.equal(fixture.document.querySelector('[data-bookmark]')?.getAttribute('aria-pressed'), 'true');
  fixture.document.getElementById('bookmark')!.click();
  (fixture.document.querySelector('.bookmark-row') as HTMLButtonElement).click();
  assert.equal(fixture.document.querySelector('.bookmark-preview strong')?.textContent, 'saved');
  assert.equal(fixture.document.querySelector('.bookmark-preview img'), null);
  assert(fixture.document.querySelector('.bookmark-jump'));
  (fixture.document.querySelector('.bookmark-preview .icon-button') as HTMLButtonElement).click();
  assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'removeBookmark', bookmarkId: 'saved' });
  snapshot.bookmarkCardIds = {}; fixture.render(snapshot);
  assert(fixture.document.querySelector('.bookmark-unavailable')); assert(fixture.document.querySelector('.bookmark-preview strong'));
  snapshot.busy = true; fixture.render(snapshot); assert((fixture.document.querySelector('[data-bookmark]') as HTMLButtonElement).disabled);
  assert.equal(parsePageMessage({ type: 'toggleBookmark', cardId: '' }), undefined);
  assert.equal(parsePageMessage({ type: 'removeBookmark', bookmarkId: 123 }), undefined);
  fixture.dom.window.close();
});

test('history search, rename and model selection use safe host messages', () => {
  const fixture = page(); const snapshot = state();
  snapshot.history = [
    { session_id: 'first', title: '<img src=x onerror=alert(1)>', updated_at: '2026-10-01T00:00:00Z' },
    { session_id: 'second', title: 'Second chat', updated_at: '2026-09-30T00:00:00Z' }
  ];
  snapshot.models = [
    { id: 'custom', name: 'My model', model: 'test', protocol: 'openai' },
    { id: 'other', name: 'Another model', model: 'test-2', protocol: 'openai' }
  ];
  snapshot.selectedModel = 'custom'; fixture.render(snapshot);
  fixture.document.getElementById('history-toggle')!.click();
  assert.equal(fixture.document.getElementById('history-panel')!.hidden, false);
  assert.equal(fixture.document.querySelector('.history-row img'), null);
  (fixture.document.querySelector('.history-rename') as HTMLButtonElement).click();
  assert.equal(fixture.messages.at(-1).type, 'renameSession');
  const search = fixture.document.getElementById('history-search') as HTMLInputElement;
  search.value = 'second'; search.dispatchEvent(new fixture.dom.window.Event('input'));
  assert.equal(fixture.document.querySelectorAll('.history-row').length, 1);
  (fixture.document.querySelector('.history-open') as HTMLButtonElement).click();
  assert.equal(fixture.messages.at(-1).sessionId, 'second');
  const select = fixture.document.getElementById('model-select') as HTMLSelectElement;
  assert.equal(select.value, 'custom'); select.value = 'other'; select.dispatchEvent(new fixture.dom.window.Event('change'));
  assert.equal(fixture.messages.at(-1).type, 'selectModel'); assert.equal(fixture.messages.at(-1).modelId, 'other');
  fixture.render(snapshot); assert.equal(select.value, 'custom');
  snapshot.busy = true; fixture.render(snapshot); assert(select.disabled);
  assert([...fixture.document.querySelectorAll<HTMLButtonElement>('.history-row button')].every(button => button.disabled));
  fixture.dom.window.close();
});

// 功能：历史采用单行名称和右侧相对时间，归档后移入独立列表并支持恢复。
// 设计：控制时钟与归档快照，验证实际按钮消息及不删除历史内容的恢复路径。
test('single-line history shows trailing time and archive or restore actions', () => {
  const fixture = page(); const snapshot = state();
  fixture.dom.window.Date.now = () => Date.parse('2026-10-02T08:00:00Z');
  snapshot.history = [
    { session_id: 'a', title: '统计 VSCode 与 TUI 连接数', updated_at: '2026-10-02T07:00:00Z' },
    { session_id: 'b', title: '分析 CCB 上下文管理', updated_at: '2026-10-02T00:00:00Z' }
  ];
  fixture.render(snapshot);
  const rows = fixture.document.querySelectorAll('.history-row');
  assert.deepEqual([...rows].map(row => row.querySelector('.history-date')!.textContent), ['1h', '8h']);
  assert.equal(rows[0].querySelector('.history-open .history-date'), null);
  assert(rows[0].querySelector('.history-trailing .history-date'));
  assert(rows[0].querySelector('.history-actions .history-rename'));
  (rows[0].querySelector('.history-archive') as HTMLButtonElement).click();
  assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'archiveSession', sessionId: 'a' });
  snapshot.archivedSessionIds = ['a']; fixture.render(snapshot);
  assert.equal(fixture.document.querySelectorAll('.history-row').length, 1);
  assert.equal(fixture.document.querySelector('.history-name')!.textContent, snapshot.history[1].title);
  fixture.document.getElementById('history-archives')!.click();
  assert.equal(fixture.document.getElementById('history-heading')!.textContent, '已归档聊天');
  assert.equal(fixture.document.querySelector('.history-name')!.textContent, snapshot.history[0].title);
  const restore = fixture.document.querySelector('.history-archive') as HTMLButtonElement;
  assert.equal(restore.textContent, '恢复'); restore.click();
  assert.deepEqual(parsePageMessage(fixture.messages.at(-1)), { type: 'restoreSession', sessionId: 'a' });
  snapshot.archivedSessionIds = []; fixture.render(snapshot);
  assert.equal(fixture.document.querySelectorAll('.history-row').length, 0);
  fixture.document.getElementById('history-archives')!.click();
  assert.equal(fixture.document.querySelectorAll('.history-row').length, 2);
  assert.equal(parsePageMessage({ type: 'archiveSession', sessionId: '' }), undefined);
  assert.equal(parsePageMessage({ type: 'restoreSession', sessionId: 123 }), undefined);
  fixture.dom.window.close();
});

test('model controls sit inside composer and show real names without default configuration labels', () => {
  const fixture = page(); const snapshot = state();
  snapshot.fallbackModel = { id: '', name: 'existing-model', model: 'existing-model', protocol: 'anthropic' };
  fixture.render(snapshot);
  const shell = fixture.document.querySelector('.composer-shell')!;
  for (const id of ['input', 'model-select', 'send', 'stop', 'settings']) assert(shell.contains(fixture.document.getElementById(id)));
  assert.equal(fixture.document.querySelector('#model-select option')!.textContent, 'existing-model');
  assert(!fixture.document.body.textContent!.includes('默认配置'));
  snapshot.models = [{ id: 'custom', name: 'My model', model: 'test', protocol: 'openai' }];
  snapshot.selectedModel = 'custom'; fixture.render(snapshot);
  assert.deepEqual([...fixture.document.querySelectorAll('#model-select option')].map(option => option.textContent), ['My model']);
  const send = fixture.document.getElementById('send')!; const stop = fixture.document.getElementById('stop')!;
  assert(!send.hidden); assert(stop.hidden);
  snapshot.busy = true; snapshot.runId = 'run'; fixture.render(snapshot);
  assert(send.hidden); assert(!stop.hidden);
  fixture.document.getElementById('settings')!.click(); assert.equal(fixture.messages.at(-1).type, 'openSettings');
  fixture.dom.window.close();
});

// 功能：真实页面渲染 Markdown 但不执行原始 HTML、脚本链接或工具输出。
// 设计：使用打包后的脚本和实际页面模板在 DOM 环境中检查攻击内容，而非只测解析函数。
test('webview sanitizes Markdown, HTML, links, and tool output', () => {
  const fixture = page(); const snapshot = state();
  snapshot.cards = [
    { id: 'answer', kind: 'assistant', text: '**你好**\n<script>window.hacked=true</script>\n[x](javascript:alert(1))\n<img src=x onerror=alert(1)>' },
    { id: 'tool', kind: 'tool', text: '', title: 'read_file', status: 'success', output: '<img onerror=alert(1)>', params: {} }
  ];
  fixture.render(snapshot);
  assert.equal(fixture.document.querySelector('.markdown strong')?.textContent, '你好');
  assert.equal(fixture.document.querySelector('.markdown script'), null);
  assert.equal(fixture.document.querySelector('.markdown img'), null);
  assert(!fixture.document.querySelector('.markdown a')?.getAttribute('href')?.includes('javascript:'));
  assert.equal(fixture.document.querySelector('.tool img'), null);
  assert(fixture.document.querySelector('.tool')!.textContent!.includes('<img onerror=alert(1)>'));
  assert.equal(fixture.document.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute('content'),
    "default-src 'none'; img-src local: data:; style-src local:; script-src 'nonce-nonce';");
  fixture.dom.window.close();
});

// 功能：页面重建恢复卡片，更新保持折叠状态，并展开完整工具输出。
// 设计：用同一快照驱动两份全新页面，模拟隐藏后重建，随后更新卡片验证 DOM 保留行为。
test('snapshot recreation, tool expansion, and details state', () => {
  const snapshot = state();
  snapshot.cards = [{ id: 'tool', kind: 'tool', title: 'read_file', text: '', output: 'x'.repeat(5000), status: 'success' }];
  for (let iteration = 0; iteration < 2; iteration++) {
    const fixture = page(); fixture.render(snapshot);
    assert.equal(fixture.messages[0].type, 'ready');
    const details = fixture.document.querySelector('details')!; details.open = true;
    snapshot.cards[0].elapsedMs = iteration + 10; fixture.render(snapshot);
    assert(fixture.document.querySelector('details')!.open);
    (fixture.document.querySelector('.tool button') as HTMLButtonElement).click();
    assert.equal(fixture.document.querySelector('.tool pre:last-of-type')!.textContent!.length, 5000);
    fixture.dom.window.close();
  }
});

// 功能：Enter 发送、Shift+Enter 换行，运行与权限终态禁用相应按钮。
// 设计：分发真实键盘事件和状态快照，验证页面提交内容与可操作状态。
test('composer keyboard and busy / permission controls', () => {
  const fixture = page(); const snapshot = state(); fixture.render(snapshot);
  const input = fixture.document.querySelector('textarea')!; input.value = 'hello';
  input.dispatchEvent(new fixture.dom.window.Event('input'));
  input.dispatchEvent(new fixture.dom.window.KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, cancelable: true }));
  assert.equal(fixture.messages.length, 1);
  input.dispatchEvent(new fixture.dom.window.KeyboardEvent('keydown', { key: 'Enter', cancelable: true }));
  assert.deepEqual(JSON.parse(JSON.stringify(fixture.messages[1])), { type: 'send', content: 'hello' });
  snapshot.busy = true; snapshot.runId = 'run'; snapshot.cards = [
    { id: 'perm', kind: 'permission', text: 'write', title: 'write_file', toolUseId: 'tool', status: 'pending' }
  ]; fixture.render(snapshot);
  assert(input.disabled); assert((fixture.document.querySelector('#new') as HTMLButtonElement).disabled);
  const button = fixture.document.querySelector('.permission button') as HTMLButtonElement; assert(!button.disabled); button.click();
  assert.equal(fixture.messages.at(-1).decision, 'allow_once');
  snapshot.cards[0].status = 'auto_deny'; fixture.render(snapshot);
  assert([...fixture.document.querySelectorAll<HTMLButtonElement>('.permission button')].every(item => item.disabled));
  snapshot.cancelling = true; fixture.render(snapshot);
  assert((fixture.document.querySelector('#stop') as HTMLButtonElement).disabled);
  fixture.dom.window.close();
});

// 功能：主题外观使用宿主变量，而不是硬编码明暗颜色。
// 设计：检查实际样式对输入框、按钮、背景和焦点的主题绑定，并验证无外部资源导入。
test('styles adapt to VS Code themes without remote resources', () => {
  const css = readFileSync('media/chat.css', 'utf8');
  for (const variable of ['--vscode-foreground', '--vscode-sideBar-background', '--vscode-button-secondaryBackground', '--vscode-input-background', '--vscode-focusBorder']) assert(css.includes(variable));
  assert(!css.includes('@import')); assert(!css.includes('http'));
});
