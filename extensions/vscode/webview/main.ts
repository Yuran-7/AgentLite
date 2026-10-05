import type { Card, ChatState, SkillSummary } from '../src/session';
import type { PageMessage } from '../src/protocol';
import { decisions } from '../src/protocol';
import { markdown } from './markdown';
import { decorateCodeBlocks } from './code-blocks';
import { installTooltips } from './tooltips';
import { installPlanInput } from './plan-input';
import { renderPlanCard } from './plan-card';
import { installSlashCommands } from './slash-commands';
import { installImages } from './images';
import { installFileMentions } from './file-mentions';
import type { WorkspaceEntry } from '../src/workspace-files';
import { installMcpPanel } from './mcp-panel';
import { effortLabels, installEffortControl } from './effort-control';
import { claudeEfforts, modelEffort } from '../src/reasoning';
import { renderFileChanges } from './file-changes';

declare function acquireVsCodeApi(): { postMessage(message: PageMessage): void };
const api = acquireVsCodeApi();
installTooltips();
const cards = document.getElementById('cards')!;
// 普通点击先收起上一次的文本选区，空白区域也能取消高亮。
document.addEventListener('pointerdown', event => {
  if (event.button === 0 && !event.shiftKey) window.getSelection()?.removeAllRanges();
});
const input = document.getElementById('input') as HTMLTextAreaElement;
const send = document.getElementById('send') as HTMLButtonElement;
const stop = document.getElementById('stop') as HTMLButtonElement;
const fresh = document.getElementById('new') as HTMLButtonElement;
const retry = document.getElementById('retry') as HTMLButtonElement;
const error = document.getElementById('error')!;
const historyPanel = document.getElementById('history-panel')!;
const historyToggle = document.getElementById('history-toggle')!;
const historySearch = document.getElementById('history-search') as HTMLInputElement;
const modelSelect = document.getElementById('model-select') as HTMLSelectElement;
const sessionTitle = document.getElementById('session-title') as HTMLButtonElement;
const modelToggle = document.getElementById('model-toggle') as HTMLButtonElement;
const modelMenu = document.getElementById('model-menu')!;
const attachmentMenu = document.getElementById('attachment-menu')!;
const attach = document.getElementById('attach') as HTMLButtonElement;
const connectionToggle = document.getElementById('connection-toggle') as HTMLButtonElement;
const connectionMenu = document.getElementById('connection-menu')!;
const permissionToggle = document.getElementById('permission-toggle') as HTMLButtonElement;
const permissionMenu = document.getElementById('permission-menu')!;
const imageInput = installImages(input, () => updateSendDisabled());
const fileMentions = installFileMentions(input, post, () => {
  updateSendDisabled();
  resizeInput();
});
const mcpPanel = installMcpPanel(input, post);
const effortControl = installEffortControl(post);
const slash = installSlashCommands(input, (id, argument) => {
  closeMenus();
  if (id === 'new') fresh.click();
  else if (id === 'history') {
    if (historyPanel.hidden) historyToggle.click();
    else historySearch.focus();
  } else if (id === 'model') {
    modelToggle.click();
    (modelMenu.querySelector('[aria-selected="true"]') as HTMLElement ?? modelMenu.querySelector('button'))?.focus();
  } else if (id === 'settings') post({ type: 'openSettings' });
  else if (id === 'mcp') mcpPanel.open();
  else if (id === 'logs') post({ type: 'openLogs' });
  else if (id === 'plan') post({ type: 'collaboration', mode: 'plan' });
  else if (id === 'compact') post({ type: 'compactSession' });
  else if (id === 'memories') openMemorySettings();
  else if (id === 'skills') {
    slash.showDetail('可用技能', '正在读取…');
    post({ type: 'refreshSkills' });
  }
  else if (id === 'reasoning') {
    if (argument !== undefined) post({ type: 'reasoning', effort: argument });
    else effortControl.open();
  }
  else if (id === 'status') slash.showDetail('当前状态', [
    `会话：${latest?.title || '新会话'}`, `会话 ID：${latest?.sessionId || '尚未创建'}`,
    `模型：${document.getElementById('model-label')!.textContent || '尚未选择'}`,
    `推理强度：${latest?.reasoningEffort || latest?.models?.find(model => model.id === latest?.selectedModel)?.reasoningEffort || (!latest?.selectedModel ? latest?.fallbackModel?.reasoningEffort : '') || '模型默认'}`,
    `连接：${document.getElementById('connection')!.textContent}`, `工作区：${latest?.workspace || '未知'}`,
    latest?.usage || '暂无用量统计',
  ].join('\n'));
}, () => ({ percent: latest?.contextPercent, estimated: latest?.contextEstimated }));
const copyShape = '<rect x="4" y="8" width="12" height="13" rx="2"/><path d="M8 8V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-2"/>';
const bookmarkShape = '<path d="M6 21V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v16l-6-4z"/>';
const bookmarkToggle = document.getElementById('bookmark') as HTMLButtonElement;
const bookmarksPanel = document.getElementById('bookmarks-panel')!;
let selectedBookmark: string | undefined;
let bookmarkSignature = '';
let latest: ChatState | undefined;
let modelSignature = '';
let historySignature = '';
let showArchivedHistory = false;
const rendered = new Map<string, { element: HTMLElement; signature: string }>();
const labels: Record<string, string> = { running: '运行中', pending: '等待确认', responding: '提交中',
  success: '成功', failed: '失败', allow_once: '允许一次', always_allow: '始终允许',
  deny_once: '拒绝一次', always_deny: '始终拒绝', auto_deny: '已自动拒绝', auto_allow: '已自动允许',
  timeout: '确认超时，已拒绝', run_cancelled: '任务已取消' };

// 在斜杠菜单的详情面板中展示当前会话的两个独立记忆开关。
function openMemorySettings(): void {
  slash.showDetail('记忆设置', '');
  const body = document.getElementById('command-body')!;
  body.replaceChildren();
  body.append(element('p', '仅作用于当前会话。生成记忆会在会话闲置后处理；使用已有记忆从下一轮对话生效。', 'memory-help'));
  for (const [setting, label] of [['generate', '生成记忆'], ['use', '使用已有记忆']] as const) {
    const button = document.createElement('button'); button.type = 'button';
    button.className = 'memory-setting'; button.dataset.memorySetting = setting;
    button.append(element('span', label), element('span', '', 'memory-setting-state'));
    button.addEventListener('click', () => {
      const enabled = setting === 'generate' ? latest?.memoryGenerateEnabled : latest?.memoryUseEnabled;
      post({ type: 'setMemory', setting, enabled: !enabled });
    });
    body.append(button);
  }
  body.append(element('div', '', 'memory-error'));
  if (latest) renderMemorySettings(latest);
}

// 在命令详情里列出技能，点击后将调用语法填入输入框。
function renderSkills(skills: SkillSummary[]): void {
  if (document.getElementById('command-detail')!.hidden ||
      document.getElementById('command-title')!.textContent !== '可用技能') return;
  slash.showDetail('可用技能', '');
  const body = document.getElementById('command-body')!;
  body.replaceChildren();
  if (!skills.length) {
    body.textContent = '当前没有可用技能。在工作区 .agentlite/skills/ 下添加 <名称>/SKILL.md。';
    return;
  }
  for (const skill of skills) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'memory-setting';
    button.append(element('span', `/${skill.name} · ${skill.description || '无描述'}`));
    button.title = skill.path;
    button.addEventListener('click', () => {
      document.getElementById('command-detail')!.hidden = true;
      input.value = `/${skill.name} `;
      input.dispatchEvent(new Event('input'));
      input.focus();
    });
    body.append(button);
  }
}

// 用服务端确认的值刷新开关，并在保存期间锁定操作。
function renderMemorySettings(state: ChatState): void {
  const body = document.getElementById('command-body')!;
  for (const button of body.querySelectorAll<HTMLButtonElement>('[data-memory-setting]')) {
    const enabled = button.dataset.memorySetting === 'generate' ? state.memoryGenerateEnabled === true : state.memoryUseEnabled !== false;
    button.setAttribute('aria-pressed', String(enabled));
    button.querySelector('.memory-setting-state')!.textContent = enabled ? '已开启' : '已关闭';
    button.disabled = state.connection !== 'ready' || !state.sessionId || state.busy || state.sending;
  }
  const error = body.querySelector<HTMLElement>('.memory-error');
  if (error) { error.textContent = state.memoryError || (state.memoryLoading ? '正在保存…' : ''); error.hidden = !error.textContent; }
}

// 将用户操作发送到扩展宿主，不在页面中访问 core。
function post(message: PageMessage): void { api.postMessage(message); }
// 根据文字、图片和已选工作区引用统一控制发送按钮。
function updateSendDisabled(): void {
  send.disabled = input.disabled || imageInput.loading() ||
    (!input.value.trim() && !imageInput.get().length && !fileMentions.references().length);
}
// 在宿主可接受新任务时发送输入。
function submit(): void {
  const references = fileMentions.references();
  if (send.disabled || imageInput.loading() || (!input.value.trim() && !imageInput.get().length && !references.length)) return;
  if (!references.length && slash.submit()) return;
  slash.close();
  const content = [input.value.trim(), ...references.map(path => /\s/.test(path) ? `@"${path}"` : `@${path}`)]
    .filter(Boolean).join(' ');
  post({ type: 'send', content, ...(imageInput.get().length ? { images: imageInput.get() } : {}),
    ...(references.length ? { references } : {}) });
  imageInput.clear(); input.value = ''; fileMentions.clear(); resizeInput(); send.disabled = true;
}
send.addEventListener('click', event => { event.stopPropagation(); submit(); });
input.addEventListener('keydown', event => {
  if (fileMentions.handleKey(event)) return;
  if (slash.handleKey(event)) return;
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit(); }
});
input.addEventListener('input', () => { updateSendDisabled(); resizeInput(); });
stop.addEventListener('click', () => post({ type: 'cancel' }));
fresh.addEventListener('click', () => post({ type: 'newSession' }));
sessionTitle.addEventListener('click', () => {
  if (!sessionTitle.disabled && latest?.sessionId) post({ type: 'renameSession', sessionId: latest.sessionId });
});
retry.addEventListener('click', () => post({ type: 'retry' }));
document.getElementById('logs')!.addEventListener('click', () => post({ type: 'openLogs' }));
document.getElementById('history-refresh')!.addEventListener('click', () => post({ type: 'refreshHistory' }));
historyToggle.addEventListener('click', () => {
  historyPanel.hidden = !historyPanel.hidden;
  historyToggle.setAttribute('aria-expanded', String(!historyPanel.hidden));
  if (!historyPanel.hidden) { post({ type: 'refreshHistory' }); historySearch.focus(); }
});
historySearch.addEventListener('input', () => { if (latest) renderHistory(latest); });
document.getElementById('history-archives')!.addEventListener('click', () => {
  showArchivedHistory = !showArchivedHistory;
  if (latest) renderHistory(latest);
});
bookmarkToggle.addEventListener('click', () => {
  bookmarksPanel.hidden = !bookmarksPanel.hidden;
  bookmarkToggle.setAttribute('aria-expanded', String(!bookmarksPanel.hidden));
  if (!bookmarksPanel.hidden) {
    if (latest) renderBookmarks(latest);
    document.getElementById('bookmarks-close')!.focus();
  }
});
// 关闭收藏时将键盘焦点交还顶部入口。
function closeBookmarks(): void {
  bookmarksPanel.hidden = true; bookmarkToggle.setAttribute('aria-expanded', 'false'); bookmarkToggle.focus();
}
document.getElementById('bookmarks-close')!.addEventListener('click', closeBookmarks);
bookmarksPanel.addEventListener('keydown', event => { if (event.key === 'Escape') closeBookmarks(); });

// 在当前会话展示收藏快照，选中后可阅读全文、取消收藏或定位原文。
function renderBookmarks(state: ChatState): void {
  const saved = state.bookmarks ?? [];
  if (!saved.some(item => item.id === selectedBookmark)) selectedBookmark = undefined;
  const body = document.getElementById('bookmarks-body')!; body.replaceChildren();
  if (!saved.length) {
    body.append(element('p', '点击模型回答下方的收藏按钮，把重要的回答留在这里。', 'bookmarks-empty')); return;
  }
  const list = element('div', '', 'bookmarks-list');
  for (const item of saved) {
    const button = document.createElement('button'); button.className = 'bookmark-row';
    button.setAttribute('aria-current', String(item.id === selectedBookmark));
    button.append(element('span', item.text.replace(/[`*#>]/g, '').replace(/\s+/g, ' ').trim(), 'bookmark-snippet'));
    const time = item.writtenAt || item.savedAt;
    if (!Number.isNaN(Date.parse(time))) button.append(element('span', new Date(time).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }), 'bookmark-time'));
    button.addEventListener('click', () => { selectedBookmark = item.id; renderBookmarks(state); });
    list.append(button);
  }
  body.append(list);
  const selected = saved.find(item => item.id === selectedBookmark);
  if (!selected) { body.append(element('p', '选择一条收藏，在这里查看完整回答。', 'bookmarks-empty')); return; }
  const preview = element('div', '', 'bookmark-preview'); const actions = element('div', '', 'bookmark-preview-actions');
  const cardId = Object.entries(state.bookmarkCardIds ?? {}).find(([, id]) => id === selected.id)?.[0];
  if (cardId && rendered.has(cardId)) {
    const jump = document.createElement('button'); jump.className = 'bookmark-jump'; jump.textContent = '在聊天中查看';
    jump.addEventListener('click', () => {
      const target = rendered.get(cardId)?.element; if (!target) return;
      if (window.matchMedia?.('(max-width: 559px)').matches) closeBookmarks();
      target.scrollIntoView?.({ block: 'center' }); target.tabIndex = -1; target.focus({ preventScroll: true });
      target.classList.add('bookmark-target'); window.setTimeout(() => target.classList.remove('bookmark-target'), 1800);
    }); actions.append(jump);
  } else actions.append(element('span', '原文已不在当前聊天展示中', 'bookmark-unavailable'));
  const remove = iconButton('取消收藏', bookmarkShape, () => {
    remove.disabled = true; post({ type: 'removeBookmark', bookmarkId: selected.id });
  }); remove.classList.add('bookmarked'); actions.append(remove); preview.append(actions);
  const content = element('div', '', 'markdown'); content.innerHTML = markdown(selected.text);
  decorateCodeBlocks(content);
  preview.append(content); body.append(preview);
}
modelSelect.addEventListener('change', () => {
  post({ type: 'selectModel', modelId: modelSelect.value }); modelSelect.disabled = true; modelToggle.disabled = true;
});

// 输入随文本和侧栏宽度增高，删减或发送后恢复单行高度。
function resizeInput(): void {
  input.style.height = 'auto';
  input.style.height = `${Math.min(180, Math.max(42, input.scrollHeight))}px`;
  input.style.overflowY = input.scrollHeight > 180 ? 'auto' : 'hidden';
}
window.addEventListener('resize', resizeInput);
if (typeof ResizeObserver !== 'undefined') {
  let previousWidth = 0;
  new ResizeObserver(entries => {
    const width = entries[0].contentRect.width;
    if (width !== previousWidth) { previousWidth = width; resizeInput(); }
  }).observe(input);
}

// 同一时刻只打开一个菜单，保留按钮的无障碍展开状态。
function closeMenus(preserveConnection = false, preserveSlash = false, preserveModel = false): void {
  if (!preserveSlash) slash.close();
  if (!preserveSlash) fileMentions.close();
  if (!preserveModel) { modelMenu.hidden = true; modelToggle.setAttribute('aria-expanded', 'false'); }
  attachmentMenu.hidden = true; attach.setAttribute('aria-expanded', 'false');
  permissionMenu.hidden = true; permissionToggle.setAttribute('aria-expanded', 'false');
  if (!preserveConnection) { connectionMenu.hidden = true; connectionToggle.setAttribute('aria-expanded', 'false'); }
}
modelToggle.addEventListener('click', () => {
  const open = modelMenu.hidden; closeMenus();
  if (open) effortControl.open(false);
  if (open) (modelMenu.querySelector('[aria-selected="true"]') ?? modelMenu.querySelector('button'))?.scrollIntoView?.({ block: 'nearest' });
});
attach.addEventListener('click', () => {
  const open = attachmentMenu.hidden; closeMenus();
  attachmentMenu.hidden = !open; attach.setAttribute('aria-expanded', String(open));
});
document.getElementById('reference-workspace-file')!.addEventListener('click', () => {
  attachmentMenu.hidden = true; attach.setAttribute('aria-expanded', 'false');
});
connectionToggle.addEventListener('click', () => {
  const open = connectionMenu.hidden; closeMenus();
  connectionMenu.hidden = !open; connectionToggle.setAttribute('aria-expanded', String(open));
});
document.addEventListener('click', event => {
  if (!(event.target as Element).closest('.model-control, .attachment-control, .connection-control, .permission-control, .file-mention-menu')) closeMenus(false, event.target === input);
  if (!historyPanel.hidden && !historyPanel.contains(event.target as Node) && !historyToggle.contains(event.target as Node)) {
    historyPanel.hidden = true; historyToggle.setAttribute('aria-expanded', 'false');
  }
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && (!modelMenu.hidden || !attachmentMenu.hidden || !connectionMenu.hidden || !permissionMenu.hidden)) {
    const target = !permissionMenu.hidden ? permissionToggle : !modelMenu.hidden ? modelToggle : !attachmentMenu.hidden ? attach : connectionToggle;
    closeMenus(); target.focus();
  } else if (event.key === 'Escape' && !historyPanel.hidden) {
    historyPanel.hidden = true; historyToggle.setAttribute('aria-expanded', 'false'); historyToggle.focus();
  }
});
modelToggle.addEventListener('keydown', event => {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault(); closeMenus(); effortControl.open(false);
    (modelMenu.querySelector('[aria-selected="true"]') as HTMLElement ?? modelMenu.querySelector('button'))?.focus();
  }
});
modelMenu.addEventListener('keydown', event => {
  if ((event.target as HTMLElement).closest('#effort-control')) return;
  const options = [...modelMenu.querySelectorAll<HTMLButtonElement>('[role="option"]:not(:disabled)')];
  let index = options.indexOf(document.activeElement as HTMLButtonElement);
  if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    index = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1
      : (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
    options[index]?.focus();
  } else if (event.key === 'Tab') closeMenus();
});

// 用总秒数生成紧凑的运行耗时文案。
function duration(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(seconds / 3600); const minutes = Math.floor(seconds / 60) % 60;
  return hours ? `${hours}h ${minutes}m ${seconds % 60}s` : minutes ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}
// 首段内容出现前显示 Thinking，之后仅保留当前聊天中的运行状态与耗时。
function updateWorking(): void {
  const hasRunSummary = !!latest?.runId && latest.cards.some(card => card.kind === 'assistant' && card.runId === latest?.runId);
  document.getElementById('run-status')!.hidden = (!latest?.busy && !latest?.compacting) || hasRunSummary;
  document.getElementById('run-label')!.textContent = latest?.cancelling ? 'Stopping…'
    : latest?.compacting ? '正在压缩上下文…' : 'Thinking…';
  for (const summary of document.querySelectorAll<HTMLElement>('[data-working-summary]')) {
    summary.textContent = latest?.cancelling ? 'Stopping…'
      : latest?.workStartedAt ? `Working for ${duration(Date.now() - latest.workStartedAt)}` : 'Working…';
  }
}
window.setInterval(updateWorking, 1000);

// 连接中实时显示等待秒数，成功后使用宿主保存的固定耗时。
function updateConnection(): void {
  if (!latest) return;
  const elapsed = latest.connectionElapsedMs ?? (latest.connectionStartedAt === undefined
    ? undefined : Math.max(0, Date.now() - latest.connectionStartedAt));
  const seconds = elapsed === undefined ? '' : `${(elapsed / 1000).toFixed(1)} 秒`;
  let text = '未连接';
  if (latest.connection === 'connecting') text = latest.connectionElapsedMs !== undefined
    ? '正在准备会话…' : `正在连接 core${seconds ? ` · 已等待 ${seconds}` : '…'}`;
  else if (latest.connection === 'ready') text = seconds ? `连接 core 花了 ${seconds}` : 'core 已连接';
  else if (latest.connection === 'error') text = `连接中断${seconds ? ` · 连接耗时 ${seconds}` : ''}`;
  document.getElementById('connection')!.textContent = text;
  connectionToggle.dataset.tooltip = `连接信息 · ${text}`;
}
window.setInterval(updateConnection, 100);

const jump = document.createElement('button'); jump.id = 'scroll-bottom'; jump.className = 'icon-button';
jump.dataset.tooltip = '回到最新消息'; jump.setAttribute('aria-label', '回到最新消息'); jump.hidden = true;
jump.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14m-6-6 6 6 6-6"/></svg>';
document.querySelector('footer')!.prepend(jump);
jump.addEventListener('click', () => { cards.scrollTop = cards.scrollHeight; jump.hidden = true; });
cards.addEventListener('scroll', () => { jump.hidden = cards.scrollHeight - cards.scrollTop - cards.clientHeight < 80; });

// 以可信的固定 SVG 构造图标按钮，消息文本不进入 HTML。
function iconButton(label: string, shape: string, action: () => void): HTMLButtonElement {
  const button = document.createElement('button'); button.className = 'icon-button';
  button.dataset.tooltip = label; button.setAttribute('aria-label', label);
  button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${shape}</svg>`;
  button.addEventListener('click', action); return button;
}

// 将会话更新时间显示为紧凑相对时间，完整日期保留在悬停提示中。
function historyTime(updatedAt: string): string {
  const timestamp = Date.parse(updatedAt);
  if (!Number.isFinite(timestamp)) return '—';
  const seconds = Math.max(0, (Date.now() - timestamp) / 1000);
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)}d`;
  return new Date(timestamp).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
}

// 单行展示会话名称和时间，悬停或键盘聚焦时露出归档与改名。
function renderHistory(state: ChatState): void {
  const list = document.getElementById('history-list')!;
  list.replaceChildren();
  const query = historySearch.value.trim().toLocaleLowerCase();
  const archived = new Set(state.archivedSessionIds ?? []);
  const entries = (state.history ?? []).filter(item => archived.has(item.session_id) === showArchivedHistory &&
    item.title.toLocaleLowerCase().includes(query));
  document.getElementById('history-heading')!.textContent = showArchivedHistory ? '已归档聊天' : '历史聊天';
  const archiveToggle = document.getElementById('history-archives')!;
  archiveToggle.textContent = showArchivedHistory ? '返回历史' : '已归档';
  archiveToggle.setAttribute('aria-pressed', String(showArchivedHistory));
  archiveToggle.setAttribute('aria-label', showArchivedHistory ? '返回历史聊天' : '查看已归档会话');
  if (!entries.length) list.append(element('div', query ? '没有匹配的会话' : showArchivedHistory
    ? '还没有归档的聊天。' : '还没有历史聊天。发送第一条消息后会自动保存。', 'history-empty'));
  for (const item of entries) {
    const row = element('div', '', `history-row${item.session_id === state.sessionId ? ' active' : ''}`);
    const open = document.createElement('button'); open.className = 'history-open';
    open.append(element('span', item.title || '未命名会话', 'history-name'));
    open.disabled = state.connection !== 'ready' || state.busy || state.sending;
    if (item.session_id === state.sessionId) open.setAttribute('aria-current', 'true');
    open.addEventListener('click', () => { post({ type: 'resumeSession', sessionId: item.session_id }); historyPanel.hidden = true; historyToggle.setAttribute('aria-expanded', 'false'); });
    const rename = document.createElement('button'); rename.className = 'history-rename'; rename.textContent = '改名';
    rename.disabled = open.disabled;
    rename.addEventListener('click', () => post({ type: 'renameSession', sessionId: item.session_id }));
    rename.setAttribute('aria-label', `改名：${item.title}`);
    const archive = document.createElement('button'); archive.className = 'history-archive';
    archive.textContent = showArchivedHistory ? '恢复' : '归档'; archive.disabled = open.disabled;
    archive.setAttribute('aria-label', `${archive.textContent}：${item.title}`);
    archive.addEventListener('click', () => post({ type: showArchivedHistory ? 'restoreSession' : 'archiveSession', sessionId: item.session_id }));
    const date = element('span', historyTime(item.updated_at), 'history-date');
    date.dataset.updatedAt = item.updated_at;
    const actions = element('div', '', 'history-actions'); actions.append(archive, rename);
    const trailing = element('div', '', 'history-trailing'); trailing.append(date, actions);
    row.append(open, trailing); list.append(row);
  }
}
window.setInterval(() => {
  for (const date of document.querySelectorAll<HTMLElement>('.history-date[data-updated-at]')) {
    date.textContent = historyTime(date.dataset.updatedAt!);
  }
}, 60_000);

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
function renderCard(card: Card, container: HTMLElement, activityOnly = false): void {
  const wasOpen = container.querySelector('details')?.open ?? false;
  container.replaceChildren();
  container.className = `card ${card.kind}`;
  if (card.kind === 'assistant') {
    container.setAttribute('aria-label', '模型回答');
    const answers = card.runId ? latest?.cards.filter(item => item.kind === 'assistant' && item.runId === card.runId) ?? [] : [card];
    const workMs = answers.find(item => item.workMs !== undefined)?.workMs;
    const completed = !card.runId || answers.some(item => item.completed || item.workMs !== undefined);
    const runCards = latest?.cards.filter(item => item.runId === card.runId) ?? [];
    const finalAnswer = completed && !['cancelled', 'failed'].includes(card.status ?? '') && (!card.runId || runCards.findLastIndex(item => item.kind === 'assistant') >
      runCards.findLastIndex(item => item.kind === 'tool'));
    if (!activityOnly && card.runId && completed) {
      const work = document.createElement('details'); work.className = 'work-summary'; work.open = wasOpen;
      const summary = element('summary', workMs === undefined ? 'Worked for · 耗时未记录' : `Worked for ${duration(workMs)}`);
      work.append(summary);
      const activity = element('div', '', 'work-activity');
      for (const item of latest?.cards.filter(item => item.runId === card.runId && item.kind !== 'user' &&
        item.kind !== 'files' && !(finalAnswer && item.id === card.id) && !(item.kind === 'permission' && item.status === 'pending')) ?? []) {
        const entry = element('article'); renderCard(item, entry, true); activity.append(entry);
      }
      if (!activity.childElementCount) activity.append(element('div', '推理与生成已完成'));
      work.append(activity); container.append(work);
      if (runCards.some(item => item.status === 'cancelled' || item.kind === 'notice' && item.text === '已停止')) {
        container.append(element('div', '用户已暂停', 'work-status'));
      }
    } else if (!activityOnly && card.runId && answers[0]?.id === card.id) {
      const progress = element('div', '', 'work-progress');
      progress.dataset.workingSummary = 'true';
      container.append(progress);
    }
    if (activityOnly || !completed || finalAnswer) {
      const match = card.text.match(/<proposed_plan>\s*([\s\S]*?)\s*<\/proposed_plan>/);
      if (match && !activityOnly) {
        const before = card.text.slice(0, match.index).trim();
        const after = card.text.slice((match.index ?? 0) + match[0].length).trim();
        if (before) { const intro = element('div', '', 'markdown'); intro.innerHTML = markdown(before); container.append(intro); }
        container.append(renderPlanCard(card, match[1], markdown, post, latest));
        if (after) { const outro = element('div', '', 'markdown'); outro.innerHTML = markdown(after); container.append(outro); }
      } else {
        const body = element('div', '', 'markdown');
        body.innerHTML = markdown(card.text.replace(/<\/?proposed_plan>/g, '')); container.append(body);
        decorateCodeBlocks(body, (blockIndex, type) => post({ type, cardId: card.id, blockIndex }));
      }
    }
    if (!activityOnly && finalAnswer && card.text.trim()) {
      const actions = element('div', '', 'answer-actions');
      for (const [type, label, shape] of [
        ['copyAnswer', '复制回答', copyShape],
        ['openAnswer', '在编辑器中展开回答', '<path d="M14 3h7v7m0-7-8 8M10 21H3v-7m0 7 8-8"/>']
      ] as const) {
        const button = document.createElement('button'); button.className = 'icon-button';
        button.dataset.tooltip = label; button.setAttribute('aria-label', label);
        if (type === 'copyAnswer') button.dataset.copy = 'true';
        button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${shape}</svg>`;
        button.addEventListener('click', () => post({ type, cardId: card.id })); actions.append(button);
      }
      const saved = !!latest?.bookmarkCardIds?.[card.id];
      const bookmark = iconButton(saved ? '取消收藏' : '收藏回答', bookmarkShape, () => {
        bookmark.disabled = true; post({ type: 'toggleBookmark', cardId: card.id });
      });
      bookmark.dataset.bookmark = 'true'; bookmark.setAttribute('aria-pressed', String(saved));
      bookmark.classList.toggle('bookmarked', saved); bookmark.disabled = !latest?.sessionId || !!latest?.busy || !!latest?.sending;
      actions.append(bookmark);
      container.append(actions);
    }
  } else if (card.kind === 'user') {
    container.setAttribute('aria-label', '用户消息');
    if (card.images?.length) {
      const gallery = element('div', '', 'image-attachments');
      for (const image of card.images) {
        if (!['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(image.media_type)) continue;
        const img = document.createElement('img'); img.src = `data:${image.media_type};base64,${image.data}`;
        img.alt = image.name; gallery.append(img);
      }
      container.append(gallery);
    }
    if (card.text) container.append(element('div', card.text, 'plain user-bubble'));
    const actions = element('div', '', 'user-actions');
    if (card.createdAt && !Number.isNaN(Date.parse(card.createdAt))) {
      const time = document.createElement('time'); time.dateTime = card.createdAt;
      time.textContent = new Date(card.createdAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      time.dataset.tooltip = new Date(card.createdAt).toLocaleString(); actions.append(time);
    }
    const copy = iconButton('Copy message', copyShape, () => post({ type: 'copyMessage', cardId: card.id }));
    copy.dataset.copy = 'true';
    const edit = iconButton('编辑并再次提问', '<path d="m14 5 5 5M4 20l5-1L20 8a2 2 0 0 0-5-5L4 14z"/>', () => {
      if (input.disabled) return;
      input.value = fileMentions.restore(card.text, card.references); imageInput.restore(card.images ?? []);
      fileMentions.refresh(); resizeInput(); updateSendDisabled(); input.focus();
    });
    edit.dataset.edit = 'true'; edit.disabled = input.disabled;
    actions.append(copy, edit); container.append(actions);
  } else if (card.kind === 'files') {
    renderFileChanges(card, container, latest, post);
  } else if (card.kind === 'tool') {
    const details = document.createElement('details'); details.open = wasOpen;
    details.append(element('summary', `${card.title} · ${labels[card.status ?? ''] ?? card.status ?? ''}${card.elapsedMs !== undefined ? ` · ${card.elapsedMs}ms` : ''}`));
    output(JSON.stringify(card.params ?? {}, null, 2), details);
    if (card.output !== undefined) output(card.output, details); container.append(details);
    if (card.autoApproved) container.append(element('span', 'auto-approved', 'muted'));
  } else if (card.kind === 'permission') {
    container.append(element('div', `权限确认 · ${card.title}`, 'role'));
    if (card.text) container.append(element('div', card.text, 'plain'));
    if (card.params) output(JSON.stringify(card.params, null, 2), container);
    container.append(element('div', labels[card.status ?? ''] ?? card.status ?? '', 'muted'));
    const actions = element('div', '', 'permission-actions');
    if (card.approvalReason) container.append(element('div', card.approvalReason, 'plain'));
    for (const decision of card.allowedDecisions ?? decisions) {
      const button = document.createElement('button'); button.textContent = labels[decision]; button.disabled = card.status !== 'pending';
      button.addEventListener('click', () => post({ type: 'permission', toolUseId: card.toolUseId!, decision })); actions.append(button);
    }
    container.append(actions);
  } else if (card.kind === 'question') {
    container.append(element('div', '计划问答', 'role'));
    container.append(element('div', card.text || (card.questions ?? []).map(question => question.question).join('\n'), 'plain'));
  } else if (card.kind === 'plan') {
    container.append(element('div', '执行计划', 'role'), element('div', card.text));
    for (const item of card.plan ?? []) container.append(element('div', `${item.status === 'completed' ? '✓' : item.status === 'in_progress' ? '◉' : '○'} ${item.step}`, 'plan-step'));
  } else if (!activityOnly && card.kind === 'notice' && card.runId && (card.completed || card.text === '已停止' || card.text.startsWith('运行失败：')) &&
      !latest?.cards.some(item => item.runId === card.runId && item.kind === 'assistant' && (item.completed || item.workMs !== undefined))) {
    container.classList.add('workflow');
    const work = document.createElement('details'); work.className = 'work-summary'; work.open = wasOpen;
    work.append(element('summary', card.workMs === undefined ? 'Worked for · 耗时未记录' : `Worked for ${duration(card.workMs)}`));
    const activity = element('div', '', 'work-activity');
    for (const item of latest?.cards.filter(item => item.runId === card.runId && item.kind !== 'user' && item.kind !== 'files' && item.id !== card.id) ?? []) {
      const entry = element('article'); renderCard(item, entry, true); activity.append(entry);
    }
    work.append(activity); container.append(work);
    if (card.status === 'cancelled' || card.text === '已停止') container.append(element('div', '用户已暂停', 'work-status'));
  } else {
    container.append(element('div', card.kind === 'subagent' ? `子 Agent · ${card.text} · ${labels[card.status ?? ''] ?? card.status}` : card.text));
  }
  if (card.runId) container.dataset.runId = card.runId;
}

// 整轮完成后把所有回答文字集中放到活动记录后面，保留各段稳定标识与原始顺序。
function orderedCards(all: Card[]): Card[] {
  const source = all.filter(card => card.kind !== 'files');
  const completed = new Set(source.filter(card => card.kind === 'assistant' && (card.completed || card.workMs !== undefined) && card.runId).map(card => card.runId!));
  const last = new Map<string, number>();
  const answers = new Map<string, Card[]>();
  source.forEach((card, index) => {
    if (!card.runId || !completed.has(card.runId)) return;
    last.set(card.runId, index);
    if (card.kind === 'assistant') {
      const group = answers.get(card.runId) ?? []; group.push(card); answers.set(card.runId, group);
    }
  });
  const result: Card[] = [];
  source.forEach((card, index) => {
    if (!(card.kind === 'assistant' && card.runId && completed.has(card.runId))) result.push(card);
    if (card.runId && last.get(card.runId) === index) result.push(...answers.get(card.runId) ?? []);
  });
  for (const card of all.filter(item => item.kind === 'files')) {
    const index = result.findLastIndex(item => item.runId === card.runId);
    if (index < 0) result.push(card); else result.splice(index + 1, 0, card);
  }
  return result;
}

// 恢复完整宿主快照，普通状态更新只重绘发生变化的卡片。
const renderPlanInput = installPlanInput(post);
const modeToggle = document.createElement('button'); modeToggle.className = 'plan-mode-toggle';
modeToggle.type = 'button'; modeToggle.hidden = true;
modeToggle.setAttribute('aria-label', '退出计划模式'); modeToggle.dataset.tooltip = '退出计划模式';
modeToggle.innerHTML = `<span class="plan-mode-icon" aria-hidden="true">
  <svg class="plan-bulb" viewBox="0 0 24 24"><path d="M9 17h6M10 20h4M9 17v-2a5 5 0 1 1 6 0v2M12 2v1M5 5l1 1M3 10h1M20 10h1M18 6l1-1"/></svg>
  <svg class="plan-dismiss" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m9 9 6 6m0-6-6 6"/></svg>
</span><span>Plan</span>`;
modeToggle.addEventListener('click', () => post({ type: 'collaboration', mode: 'default' }));
modelToggle.parentElement!.after(modeToggle);
for (const [value, label, description, icon] of [
  ['manual', 'Manual', '遵循现有审批策略，执行操作前请求确认。', '<path d="M8 13V6a2 2 0 0 1 4 0v7M12 12V4a2 2 0 0 1 4 0v9M16 12V7a2 2 0 0 1 4 0v9c0 4-3 6-7 6-3 0-5-2-7-5l-3-4a2 2 0 0 1 3-3l2 3"/>'],
  ['accept_edits', 'Edit automatically', '自动编辑工作区普通文件，其他操作遵循审批策略。', '<path d="m7 6-6 6 6 6m10-12 6 6-6 6M14 3l-4 18"/>'],
  ['auto', 'Auto', '自动批准安全且符合用户意图的操作，风险或不确定操作请求确认。', '<path d="m13 2-10 12h8l-1 8 11-13h-8z"/>'],
] as const) {
  const button = document.createElement('button'); button.type = 'button';
  button.dataset.permissionMode = value; button.setAttribute('role', 'menuitemradio');
  button.innerHTML = `<svg class="permission-option-icon" viewBox="0 0 24 24" aria-hidden="true">${icon}</svg><span class="permission-option-copy"><span class="permission-option-label">${label}</span><span class="permission-option-description">${description}</span></span><svg class="permission-option-check" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>`;
  button.addEventListener('click', () => {
    if (button.disabled) return;
    post({ type: 'permissionMode', mode: value }); closeMenus(); permissionToggle.focus();
  });
  permissionMenu.append(button);
}
// 展开权限菜单，并将键盘焦点放在当前选项。
function openPermissionMenu(): void {
  closeMenus(); permissionMenu.hidden = false; permissionToggle.setAttribute('aria-expanded', 'true');
  (permissionMenu.querySelector('[aria-checked="true"]') as HTMLButtonElement ?? permissionMenu.querySelector('button'))?.focus();
}
permissionToggle.addEventListener('click', () => {
  if (permissionMenu.hidden) openPermissionMenu(); else closeMenus();
});
permissionToggle.addEventListener('keydown', event => {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); openPermissionMenu(); }
});
permissionMenu.addEventListener('keydown', event => {
  const options = [...permissionMenu.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
  const index = options.indexOf(document.activeElement as HTMLButtonElement);
  if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    options[event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1
      : (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length]?.focus();
  } else if (event.key === 'Tab') closeMenus();
});
// 同步界面与问答弹窗。
function render(state: ChatState): void {
  renderPlanInput(state);
  modeToggle.hidden = state.collaborationMode !== 'plan';
  const modeBusy = state.busy || state.sending || state.cards.some(card => card.kind === 'subagent' && card.status === 'running');
  permissionToggle.disabled = state.connection !== 'ready' || !!state.modeChanging || (state.collaborationMode === 'plan' && modeBusy);
  for (const button of permissionMenu.querySelectorAll<HTMLButtonElement>('button')) {
    button.setAttribute('aria-checked', String(button.dataset.permissionMode === (state.permissionMode ?? 'manual')));
    button.disabled = permissionToggle.disabled;
  }
  const modeLabel = permissionMenu.querySelector('[aria-checked="true"] .permission-option-label')!.textContent;
  permissionToggle.dataset.tooltip = `权限模式 · ${modeLabel}`;
  permissionToggle.setAttribute('aria-label', `权限模式 · ${modeLabel}`);
  if (permissionToggle.disabled) { permissionMenu.hidden = true; permissionToggle.setAttribute('aria-expanded', 'false'); }
  modeToggle.disabled = modeBusy || state.connection !== 'ready';
  if (latest?.sessionId !== state.sessionId) imageInput.clear();
  latest = state;
  mcpPanel.render(state);
  effortControl.render(state);
  renderMemorySettings(state);
  sessionTitle.textContent = state.title || '新会话';
  sessionTitle.setAttribute('aria-label', `${state.title || '新会话'} · 点击重命名`);
  document.getElementById('workspace')!.textContent = state.workspace;
  updateConnection();
  connectionToggle.dataset.state = state.connection;
  const counts = document.getElementById('connection-counts')!;
  counts.hidden = state.connection !== 'ready';
  counts.textContent = state.frontendCounts
    ? `VS Code 插件：${state.frontendCounts.vscode} · TUI：${state.frontendCounts.tui}`
    : '连接数量暂不可用';
  document.getElementById('stats')!.textContent = state.usage;
  error.hidden = !state.error; error.textContent = state.error ?? '';
  const locked = state.connection !== 'ready' || state.busy || state.sending || !!state.mcpBusy;
  sessionTitle.disabled = locked || !state.sessionId;
  const nextModelSignature = JSON.stringify([state.models, state.selectedModel, state.fallbackModel, state.model]);
  if (nextModelSignature !== modelSignature) {
    modelSelect.replaceChildren();
    const choices = [...(state.models ?? [])];
    if ((!choices.length || !state.selectedModel) && state.fallbackModel) choices.push(state.fallbackModel);
    for (const model of choices) {
      const option = document.createElement('option'); option.value = model.id;
      option.textContent = model.name || model.model; modelSelect.append(option);
    }
    if (state.selectedModel && !(state.models ?? []).some(model => model.id === state.selectedModel)) {
      const missing = document.createElement('option'); missing.value = state.selectedModel; missing.textContent = `${state.selectedModel} · 配置已移除`; modelSelect.append(missing);
    }
    if (!modelSelect.options.length) {
      const placeholder = document.createElement('option'); placeholder.value = '';
      placeholder.textContent = state.model || '选择模型'; placeholder.disabled = true; modelSelect.append(placeholder);
    }
    modelSelect.value = state.selectedModel ?? ''; modelSignature = nextModelSignature;
  }
  modelSelect.disabled = locked;
  modelSelect.value = state.selectedModel ?? '';
  modelToggle.disabled = locked;
  const currentModel = modelSelect.selectedOptions[0] ?? modelSelect.options[0];
  document.getElementById('model-label')!.textContent = currentModel?.textContent || state.model || '选择模型';
  const profile = state.models?.find(model => model.id === state.selectedModel) ?? state.fallbackModel;
  const effort = modelEffort(state.reasoningOptions?.model || profile?.model || '', state.reasoningEffort || state.reasoningOptions?.effectiveEffort || profile?.reasoningEffort);
  const effortBadge = document.getElementById('model-effort')!;
  effortBadge.hidden = profile?.protocol !== 'openai' &&
    !(profile?.protocol === 'anthropic' && claudeEfforts(profile.model).length > 0);
  effortBadge.textContent = effortLabels[effort] || effort;
  const options = document.getElementById('model-options')!;
  const signature = JSON.stringify([nextModelSignature, locked]);
  if (options.dataset.signature !== signature) {
    options.replaceChildren(); options.dataset.signature = signature;
    for (const option of modelSelect.options) {
      const button = document.createElement('button'); button.className = 'model-option';
      button.setAttribute('role', 'option'); button.setAttribute('aria-selected', String(option === currentModel));
      button.disabled = locked || option.disabled;
      const description = element('span', '', 'model-description');
      const profile = state.models?.find(model => model.id === option.value) ?? state.fallbackModel;
      description.textContent = profile && profile.model !== option.textContent ? profile.model : 'Custom model';
      const label = element('span', '', 'model-option-label'); label.append(element('span', option.textContent || ''), description);
      button.append(label, element('span', option === currentModel ? '✓' : '', 'model-check'));
      button.addEventListener('click', () => {
        modelSelect.value = option.value; modelSelect.dispatchEvent(new Event('change')); modelToggle.focus();
      }); options.append(button);
    }
  }
  if (locked) closeMenus(true, false, !!state.reasoningLoading || !!state.modelChanging);
  const nextHistorySignature = JSON.stringify([state.history, state.archivedSessionIds, state.sessionId, locked]);
  if (historySignature !== nextHistorySignature) { renderHistory(state); historySignature = nextHistorySignature; }
  input.disabled = locked; slash.refresh(); fileMentions.refresh(); imageInput.render(); updateSendDisabled();
  for (const record of rendered.values()) {
    const edit = record.element.querySelector<HTMLButtonElement>('[data-edit]'); if (edit) edit.disabled = locked;
  }
  fresh.disabled = locked; retry.disabled = state.connection === 'connecting' || state.busy || state.sending;
  stop.disabled = !state.busy || !state.runId || state.cancelling;
  stop.hidden = !state.busy; send.hidden = state.busy;
  const atBottom = cards.scrollHeight - cards.scrollTop - cards.clientHeight < 80;
  const ids = new Set(state.cards.map(card => card.id));
  for (const [id, record] of rendered) if (!ids.has(id)) { record.element.remove(); rendered.delete(id); }
  if (!state.cards.length) {
    if (!cards.querySelector('.empty')) {
      const welcome = element('div', '', 'empty');
      const logo = document.createElement('img'); logo.className = 'empty-logo';
      logo.src = cards.dataset.logo || 'logo.svg'; logo.alt = 'AgentLite'; logo.draggable = false;
      welcome.append(logo, element('div', '你想完成什么？', 'empty-title'), element('div', '提问、探索代码，或一起完成一个修改。'));
      cards.append(welcome);
    }
  } else cards.querySelector('.empty')?.remove();
  let position = cards.firstElementChild;
  for (const card of orderedCards(state.cards)) {
    let record = rendered.get(card.id);
    if (!record) { record = { element: element('article'), signature: '' }; rendered.set(card.id, record); cards.append(record.element); }
    if (record.element !== position) cards.insertBefore(record.element, position);
    position = record.element.nextElementSibling;
    const runCards = (card.kind === 'assistant' || card.kind === 'notice') && card.runId ? state.cards.filter(item => item.runId === card.runId) : undefined;
    const signature = JSON.stringify([card, runCards, state.bookmarkCardIds?.[card.id], state.busy, state.sending, state.connection, state.collaborationMode, state.permissionMode]);
    const answers = card.runId ? state.cards.filter(answer => answer.kind === 'assistant' && answer.runId === card.runId) : [];
    const completed = answers.some(answer => answer.completed || answer.workMs !== undefined);
    const visibleAnswer = completed ? answers.at(-1) : undefined;
    const terminalNotice = card.runId ? state.cards.findLast(item => item.runId === card.runId && item.kind === 'notice' &&
      (item.completed || item.text === '已停止' || item.text.startsWith('运行失败：'))) : undefined;
    const summaryCard = visibleAnswer ?? terminalNotice;
    if (signature !== record.signature) { renderCard(card, record.element, card.kind === 'assistant' && !!visibleAnswer && card.id !== visibleAnswer.id); record.signature = signature; }
    record.element.hidden = !!summaryCard && card.kind !== 'user' && card.kind !== 'files' &&
      card.id !== summaryCard.id && !(card.kind === 'permission' && card.status === 'pending');
  }
  cards.append(document.getElementById('run-status')!);
  updateWorking();
  if (atBottom) cards.scrollTop = cards.scrollHeight;
  jump.hidden = atBottom || !state.cards.length;
  const savedSignature = JSON.stringify([state.sessionId, state.bookmarks, state.bookmarkCardIds]);
  if (savedSignature !== bookmarkSignature) {
    renderBookmarks(state); bookmarkSignature = savedSignature;
  }
}

window.addEventListener('message', event => {
  const message = event.data;
  if (message?.type === 'state') render(message.state as ChatState);
  else if (message?.type === 'skills' && Array.isArray(message.skills)) renderSkills(message.skills as SkillSummary[]);
  else if (message?.type === 'skillsError' &&
      document.getElementById('command-title')!.textContent === '可用技能') {
    document.getElementById('command-body')!.textContent = `读取技能失败：${String(message.message)}`;
  }
  else if (message?.type === 'workspaceFiles' && typeof message.requestId === 'number' && Array.isArray(message.files)) {
    fileMentions.receive(message.requestId, message.files.filter((entry: unknown): entry is WorkspaceEntry => {
      if (!entry || typeof entry !== 'object') return false;
      const value = entry as Record<string, unknown>;
      return typeof value.path === 'string' && value.path.length <= 1024 &&
        (value.kind === 'file' || value.kind === 'directory');
    }));
  }
  else if (message?.type === 'workspaceFilesError' && typeof message.requestId === 'number') {
    fileMentions.failed(message.requestId, `读取工作区文件失败：${String(message.message || '未知错误')}`);
  }
  else if (message?.type === 'bookmarksSettled') {
    for (const button of document.querySelectorAll<HTMLButtonElement>('[data-bookmark]')) {
      button.disabled = !latest?.sessionId || !!latest?.busy || !!latest?.sending;
    }
    const remove = document.querySelector<HTMLButtonElement>('.bookmark-preview .icon-button');
    if (remove) remove.disabled = false;
  }
  else if (message?.type === 'copied') {
    const button = rendered.get(message.cardId)?.element.querySelector<HTMLButtonElement>('[data-copy]');
    if (button) {
      button.dataset.tooltip = '已复制'; button.setAttribute('aria-label', '已复制');
      button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>';
      window.setTimeout(() => {
        if (!button.isConnected) return;
        const label = button.closest('.user') ? 'Copy message' : '复制回答';
        button.dataset.tooltip = label; button.setAttribute('aria-label', label);
        button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${copyShape}</svg>`;
      }, 2000);
    }
  }
  else if (message?.type === 'unavailable') {
    closeMenus();
    if (latest) mcpPanel.render({ ...latest, connection: 'error', mcpBusy: false, mcpLoading: false });
    error.hidden = false; error.textContent = message.message;
    input.disabled = true; send.disabled = true; fresh.disabled = true; stop.disabled = true; retry.disabled = false;
    sessionTitle.disabled = true;
    modelToggle.disabled = true; modelSelect.disabled = true; closeMenus();
  }
});
post({ type: 'ready' });
resizeInput();
