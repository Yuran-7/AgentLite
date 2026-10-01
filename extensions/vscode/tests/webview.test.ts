import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import { webviewHtml } from '../src/html';
import type { ChatState } from '../src/session';

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

// 功能：真实页面渲染 Markdown 但不执行原始 HTML、脚本链接或工具输出。
// 设计：使用打包后的脚本和实际页面模板在 DOM 环境中检查攻击内容，而非只测解析函数。
test('webview sanitizes Markdown, HTML, links, and tool output', () => {
  const fixture = page(); const snapshot = state();
  snapshot.cards = [
    { id: 'answer', kind: 'assistant', text: '**你好**\n<script>window.hacked=true</script>\n[x](javascript:alert(1))\n<img src=x onerror=alert(1)>' },
    { id: 'tool', kind: 'tool', text: '', title: 'read_file', status: 'success', output: '<img onerror=alert(1)>', params: {} }
  ];
  fixture.render(snapshot);
  assert.equal(fixture.document.querySelector('strong')?.textContent, '你好');
  assert.equal(fixture.document.querySelector('.markdown script'), null);
  assert.equal(fixture.document.querySelector('.markdown img'), null);
  assert(!fixture.document.querySelector('.markdown a')?.getAttribute('href')?.includes('javascript:'));
  assert.equal(fixture.document.querySelector('.tool img'), null);
  assert(fixture.document.querySelector('.tool')!.textContent!.includes('<img onerror=alert(1)>'));
  assert.equal(fixture.document.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute('content'),
    "default-src 'none'; style-src local:; script-src 'nonce-nonce';");
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
  for (const variable of ['--vscode-foreground', '--vscode-sideBar-background', '--vscode-button-background', '--vscode-input-background', '--vscode-focusBorder']) assert(css.includes(variable));
  assert(!css.includes('@import')); assert(!css.includes('http'));
});
