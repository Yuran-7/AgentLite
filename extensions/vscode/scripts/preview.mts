// Generate a local preview using the actual Webview template and bundled script.
import { writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { webviewHtml } from '../src/html';
import type { ChatState } from '../src/session';

const state: ChatState = {
  workspace: 'C:\\Projects\\AgentLite', title: '改进插件的聊天体验', sessionId: 'current',
  connection: 'ready', busy: false, sending: false, cancelling: false,
  model: '', usage: '输入 2,416 · 输出 386 · 上下文 3.2%', selectedModel: 'custom',
  models: [{ id: 'custom', name: 'gpt-6.1-sol', model: 'custom-model', protocol: 'openai' },
    { id: 'other-model', name: '我的 Anthropic 模型', model: 'custom-anthropic', protocol: 'anthropic' }],
  history: [
    { session_id: 'current', title: '改进插件的聊天体验', updated_at: '2026-10-01T08:30:00Z' },
    { session_id: 'other', title: '梳理项目结构和启动流程', updated_at: '2026-09-30T10:15:00Z' }
  ],
  cards: [
    { id: 'user', kind: 'user', text: '帮我整理一下插件的配置方式，方便切换自己的模型。' },
    { id: 'tool', kind: 'tool', title: 'read_file', params: { path: '~/.agentlite/settings.json' }, status: 'success', elapsedMs: 12, text: '', output: '{ "models": [] }' },
    { id: 'assistant', kind: 'assistant', text: '配置可以分为两个文件：\n\n- **settings.json**：模型名称、协议和接口地址。\n- **.env**：API 密钥，通过 `apiKeyEnv` 引用。\n\n在下方选择模型后，可以继续当前对话。聊天记录会自动保存，方便随时回来。' },
    { id: 'notice', kind: 'notice', text: '已完成' }
  ]
};
state.cards[0].createdAt = '2026-10-02T04:15:00Z';
if (process.argv.includes('--worked')) {
  state.cards = state.cards.filter(card => card.kind !== 'notice');
  for (const card of state.cards) if (card.kind !== 'user') card.runId = 'preview-run';
  state.cards.find(card => card.kind === 'assistant')!.workMs = 261_000;
}
if (process.argv.includes('--greeting')) {
  state.cards = [{ id: 'user', kind: 'user', text: 'hello' },
    { id: 'assistant', kind: 'assistant', text: '你好！有什么需要我帮忙的？' }];
  state.usage = '';
}
if (process.argv.includes('--empty')) { state.cards = []; state.usage = ''; state.title = '新会话'; }
if (process.argv.includes('--bookmarks')) {
  const answer = state.cards.find(card => card.kind === 'assistant');
  if (answer) {
    state.bookmarks = [{ id: 'saved-answer', sessionId: 'current', sourceKey: 'preview', text: answer.text, savedAt: '2026-10-02T04:30:00Z' }];
    state.bookmarkCardIds = { [answer.id]: 'saved-answer' };
  }
}
const variables = `@layer vscode-default {
  html { scrollbar-color: var(--vscode-scrollbarSlider-background) var(--vscode-editor-background); }
  body { padding: 0 20px; }
}
:root {
  --vscode-foreground: #d5dae3; --vscode-sideBar-background: #181b22;
  --vscode-editor-background: #15181f; --vscode-font-family: 'Segoe UI', sans-serif;
  --vscode-font-size: 13px; --vscode-panel-border: #303540;
  --vscode-descriptionForeground: #8e98aa; --vscode-button-background: #277c68;
  --vscode-button-foreground: #ffffff; --vscode-button-hoverBackground: #318e79;
  --vscode-button-secondaryForeground: #d5dae3; --vscode-button-secondaryBackground: #2a303b;
  --vscode-button-secondaryHoverBackground: #343c49; --vscode-input-background: #222731;
  --vscode-input-foreground: #d5dae3; --vscode-input-border: #394252;
  --vscode-dropdown-background: #222731; --vscode-dropdown-foreground: #d5dae3;
  --vscode-dropdown-border: #394252; --vscode-focusBorder: #58baa0;
  --vscode-toolbar-hoverBackground: #29313b; --vscode-list-inactiveSelectionBackground: #263b37;
  --vscode-textCodeBlock-background: #222731; --vscode-editor-font-family: Consolas, monospace;
  --vscode-textLink-foreground: #64cdb4;
  --vscode-scrollbarSlider-background: #ffffff66;
}`;
const extraStyles = `${process.argv.includes('--hover') ? '.user-actions { opacity: 1; pointer-events: auto; }' : ''}
${process.argv.includes('--narrow') ? 'body { width: 320px; } .user { max-width: 88%; margin-right: 16px; } #bookmarks-panel { position: absolute; inset: 0; z-index: 25; width: 100%; max-width: none; min-width: 0; border-left: 0; }' : ''}
${process.argv.includes('--light') ? ':root { --vscode-foreground: #333; --vscode-sideBar-background: #f8f8f8; --vscode-editor-background: #fff; --vscode-editorWidget-background: #fff; --vscode-input-background: #eee; --vscode-input-foreground: #333; --vscode-descriptionForeground: #777; --vscode-panel-border: #ddd; --vscode-toolbar-hoverBackground: #eee; --vscode-textCodeBlock-background: #eee; }' : ''}`;
writeFileSync('dist/preview-theme.css', variables + extraStyles);
let html = webviewHtml('preview', 'file:', 'webview.js', '../media/chat.css', '../media/logo.svg');
html = html.replace('</head>', '<link rel="stylesheet" href="preview-theme.css"></head>');
html = html.replace('<script nonce="preview"', '<script nonce="preview">window.acquireVsCodeApi = () => ({postMessage() {}});</script><script nonce="preview"');
html = html.replace('</body>', `<script nonce="preview">window.dispatchEvent(new MessageEvent('message', { data: { type: 'state', state: ${JSON.stringify(state)} } }));${process.argv.includes('--history') ? "document.getElementById('history-toggle').click();" : ''}${process.argv.includes('--menu') ? "document.getElementById('model-toggle').click();" : ''}${process.argv.includes('--bookmarks') || process.argv.includes('--bookmarks-empty') ? "document.getElementById('bookmark').click(); document.querySelector('.bookmark-row')?.click();" : ''}${process.argv.includes('--connection') ? "document.getElementById('connection-toggle').click();" : ''}${process.argv.includes('--multiline') ? "const input = document.getElementById('input'); input.value = '请分析当前项目的结构。\\n检查入口与配置。\\n列出需要修改的文件。'; input.dispatchEvent(new Event('input'));" : ''}</script></body>`);
writeFileSync('dist/preview.html', html);
console.log(resolve('dist/preview.html'));
