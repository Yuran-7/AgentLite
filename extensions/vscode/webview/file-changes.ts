import type { Card, ChatState } from '../src/session';
import type { PageMessage } from '../src/protocol';

const expanded = new Set<string>();

// 使用纯文本创建节点，文件名不能注入页面标记。
function node(tag: string, className: string, text = ''): HTMLElement {
  const result = document.createElement(tag); result.className = className; result.textContent = text; return result;
}

// 以绿色和红色分别显示增删行数，读屏标签保留明确含义。
function stats(added: number, removed: number): HTMLElement {
  const result = node('span', 'file-change-stats'); result.setAttribute('aria-label', `新增 ${added} 行，删除 ${removed} 行`);
  result.append(node('span', 'lines-added', `+${added}`), node('span', 'lines-removed', `-${removed}`)); return result;
}

// 展示本轮文件汇总，默认三行并保留展开状态，操作只提交宿主卡片标识。
export function renderFileChanges(card: Card, container: HTMLElement, state: ChatState | undefined,
  post: (message: PageMessage) => void): void {
  const summary = card.fileSummary; if (!summary?.files.length) return;
  const panel = node('section', 'file-changes'); panel.setAttribute('aria-label', '文件修改');
  const header = node('div', 'file-changes-header');
  const icon = node('span', 'file-changes-icon'); icon.setAttribute('aria-hidden', 'true');
  icon.innerHTML = '<svg viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="18" rx="3"/><path d="M8 10h8M12 6v8M8 17h8"/></svg>';
  const title = node('div', 'file-changes-title');
  title.append(node('strong', '', `已修改 ${summary.files.length} 个文件`),
    stats(summary.files.reduce((n, f) => n + f.added, 0), summary.files.reduce((n, f) => n + f.removed, 0)));
  const actions = node('div', 'file-changes-actions');
  const undo = document.createElement('button'); undo.className = 'file-changes-undo';
  undo.textContent = summary.busy ? '撤销中…' : summary.undone ? '已撤销' : '撤销 ↶';
  undo.disabled = summary.undone || !!summary.busy || !!state?.busy || !!state?.sending || state?.connection !== 'ready';
  undo.addEventListener('click', () => post({ type: 'undoFileChanges', cardId: card.id }));
  const view = document.createElement('button'); view.className = 'file-changes-view'; view.textContent = '查看更改';
  view.addEventListener('click', () => post({ type: 'viewFileChanges', cardId: card.id }));
  actions.append(undo, view); header.append(icon, title, actions); panel.append(header);
  const list = node('div', 'file-changes-list');
  for (const file of summary.files.slice(0, expanded.has(card.id) ? undefined : 3)) {
    const row = document.createElement('button'); row.className = 'file-change-row'; row.setAttribute('aria-label', file.path);
    const root = state?.workspace.replaceAll('\\', '/').replace(/\/$/, '') ?? '';
    const normalized = file.path.replaceAll('\\', '/');
    const path = normalized.toLowerCase().startsWith(`${root.toLowerCase()}/`) ? normalized.slice(root.length + 1) : normalized;
    const name = node('span', 'file-change-path');
    const slash = path.lastIndexOf('/');
    if (slash >= 0) name.append(node('span', 'file-change-directory', path.slice(0, slash + 1)));
    name.append(node('span', 'file-change-name', path.slice(slash + 1)));
    row.append(name, stats(file.added, file.removed));
    row.addEventListener('click', () => post({ type: 'viewFileChanges', cardId: card.id, path: file.path })); list.append(row);
  }
  if (summary.files.length > 3) {
    const more = document.createElement('button'); more.className = 'file-changes-more';
    more.textContent = expanded.has(card.id) ? '收起 ⌃' : `再显示 ${summary.files.length - 3} 个文件 ⌄`;
    more.setAttribute('aria-expanded', String(expanded.has(card.id)));
    more.addEventListener('click', () => {
      if (expanded.has(card.id)) expanded.delete(card.id); else expanded.add(card.id);
      container.replaceChildren(); renderFileChanges(card, container, state, post);
    }); list.append(more);
  }
  panel.append(list);
  if (summary.error) panel.append(node('div', 'file-changes-error', summary.error));
  container.append(panel);
}
