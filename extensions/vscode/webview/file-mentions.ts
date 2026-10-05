import type { PageMessage } from '../src/protocol';
import type { WorkspaceEntry } from '../src/workspace-files';

// 在输入框中选择工作区文件，并保留明确的文件引用供宿主验证。
export function installFileMentions(input: HTMLTextAreaElement, post: (message: PageMessage) => void,
  changed: () => void) {
  const menu = document.getElementById('file-mention-menu')!;
  const options = document.getElementById('file-mention-options')!;
  const search = document.getElementById('file-mention-search') as HTMLInputElement;
  const empty = document.getElementById('file-mention-empty')!;
  const picker = document.getElementById('reference-workspace-file') as HTMLButtonElement;
  const error = document.getElementById('file-reference-error')!;
  const chips = document.getElementById('file-reference-chips')!;
  const selected = new Map<string, WorkspaceEntry['kind']>();
  const placeholder = input.placeholder;
  let token: { start: number; end: number; query: string } | undefined;
  let mode: 'inline' | 'picker' | undefined;
  let entries: WorkspaceEntry[] = [];
  let active = 0;
  let requestId = 0;
  let timer: number | undefined;
  let deadline: number | undefined;
  let loading = false;
  let searchError = '';

  const mention = (path: string) => /\s/.test(path) ? `@"${path}"` : `@${path}`;
  // 返回独立显示在输入框里的工作区引用。
  const references = () => [...selected.keys()];
  // 用蓝色目录或文件图标显示已选引用，点击可移除。
  const renderChips = () => {
    chips.replaceChildren(); chips.hidden = selected.size === 0;
    input.placeholder = selected.size ? '' : placeholder;
    for (const [path, kind] of selected) {
      const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'file-reference-chip';
      chip.title = `${path} · 点击移除引用`;
      chip.setAttribute('aria-label', `移除引用 ${path}`); chip.disabled = input.disabled;
      const icon = document.createElement('span'); icon.setAttribute('aria-hidden', 'true');
      icon.innerHTML = kind === 'directory'
        ? '<svg viewBox="0 0 24 24"><path d="M2.5 6.5A2.5 2.5 0 0 1 5 4h5l2 2h7a2.5 2.5 0 0 1 2.5 2.5v10A2.5 2.5 0 0 1 19 21H5a2.5 2.5 0 0 1-2.5-2.5z"/></svg>'
        : '<svg viewBox="0 0 24 24"><path d="M5 2.5h9l5 5v14H5zM14 2.5v5h5"/></svg>';
      const label = document.createElement('span'); label.textContent = path.replace(/\/$/, '').split('/').at(-1)!;
      chip.append(icon, label);
      chip.addEventListener('click', () => { selected.delete(path); renderChips(); changed(); input.focus(); });
      chips.append(chip);
    }
  };
  const close = () => {
    menu.hidden = true; token = undefined; mode = undefined;
    if (timer !== undefined) window.clearTimeout(timer);
    if (deadline !== undefined) window.clearTimeout(deadline);
    requestId++; input.setAttribute('aria-expanded', 'false');
  };
  const currentToken = () => {
    const before = input.value.slice(0, input.selectionStart);
    const match = /(?:^|\s)@([^\s@]*)$/.exec(before);
    if (!match) return;
    return { start: before.length - match[1].length - 1, end: input.selectionStart, query: match[1] };
  };
  const render = () => {
    options.replaceChildren();
    entries.forEach((entry, index) => {
      const button = document.createElement('button'); button.type = 'button'; button.role = 'option';
      button.className = 'file-mention-row'; button.title = entry.path;
      button.setAttribute('aria-label', `${entry.kind === 'directory' ? '目录' : '文件'} ${entry.path}`);
      button.setAttribute('aria-selected', String(index === active));
      const icon = document.createElement('span'); icon.className = 'file-mention-icon'; icon.setAttribute('aria-hidden', 'true');
      icon.innerHTML = entry.kind === 'directory'
        ? '<svg viewBox="0 0 24 24"><path d="M2.5 6.5A2.5 2.5 0 0 1 5 4h5l2 2h7a2.5 2.5 0 0 1 2.5 2.5v10A2.5 2.5 0 0 1 19 21H5a2.5 2.5 0 0 1-2.5-2.5z"/></svg>'
        : '<svg viewBox="0 0 24 24"><path d="M5 2.5h9l5 5v14H5zM14 2.5v5h5"/></svg>';
      const name = document.createElement('span'); name.className = 'file-mention-name';
      name.textContent = entry.path.replace(/\/$/, '').split('/').at(-1)!;
      const parent = document.createElement('span'); parent.className = 'file-mention-parent';
      const parts = entry.path.replace(/\/$/, '').split('/'); parent.textContent = parts.slice(0, -1).join('/');
      button.append(icon, name, parent);
      button.addEventListener('mousedown', event => event.preventDefault());
      button.addEventListener('click', () => choose(entry)); options.append(button);
    });
    search.hidden = mode !== 'picker';
    menu.hidden = mode === undefined;
    empty.hidden = !!entries.length;
    if (!empty.hidden) empty.textContent = searchError || (loading ? '正在搜索工作区文件…' : '没有匹配的文件');
    input.setAttribute('aria-expanded', String(mode === 'inline' && !menu.hidden));
  };
  const choose = (entry: WorkspaceEntry) => {
    const { path, kind } = entry;
    if (selected.size >= 20 && !selected.has(path)) {
      error.textContent = '每条消息最多引用 20 个文件'; error.hidden = false; close(); return;
    }
    error.hidden = true;
    const range = token;
    if (range) input.setRangeText('', range.start, range.end, 'end');
    selected.set(path, kind); close(); renderChips(); input.focus(); changed();
  };
  const update = () => {
    if (input.disabled) { close(); return; }
    token = currentToken();
    if (!token) { close(); return; }
    mode = 'inline';
    entries = []; active = 0; loading = true; searchError = ''; render();
    if (timer !== undefined) window.clearTimeout(timer);
    if (deadline !== undefined) window.clearTimeout(deadline);
    const id = ++requestId;
    timer = window.setTimeout(() => {
      post({ type: 'searchWorkspaceFiles', query: token!.query, requestId: id });
      deadline = window.setTimeout(() => {
        if (id === requestId && loading) { searchError = '搜索超时，请重试'; loading = false; render(); }
      }, 8000);
    }, 120);
  };
  input.addEventListener('input', update);
  input.addEventListener('click', update);
  input.addEventListener('compositionstart', close);
  picker.addEventListener('click', () => {
    if (input.disabled) return;
    close(); mode = 'picker'; entries = []; active = 0; loading = true; searchError = ''; search.value = ''; render(); search.focus();
    post({ type: 'searchWorkspaceFiles', query: '', requestId: ++requestId });
  });
  search.addEventListener('input', () => {
    if (mode !== 'picker') return;
    entries = []; loading = true; searchError = ''; render();
    if (timer !== undefined) window.clearTimeout(timer);
    if (deadline !== undefined) window.clearTimeout(deadline);
    const id = ++requestId;
    timer = window.setTimeout(() => post({ type: 'searchWorkspaceFiles', query: search.value, requestId: id }), 120);
  });
  search.addEventListener('keydown', event => {
    if (mode !== 'picker') return;
    if (event.key === 'Escape') { event.preventDefault(); close(); input.focus(); }
    else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (entries.length) { active = (active + (event.key === 'ArrowDown' ? 1 : -1) + entries.length) % entries.length; render(); }
    } else if (event.key === 'Enter' && entries[active]) { event.preventDefault(); choose(entries[active]); }
  });
  return {
    close,
    handleKey: (event: KeyboardEvent) => {
      if (event.key === 'Backspace' && !input.value && selected.size && !event.isComposing) {
        event.preventDefault(); selected.delete(references().at(-1)!); renderChips(); changed(); return true;
      }
      if (menu.hidden || mode !== 'inline' || event.isComposing) return false;
      if (event.key === 'Escape') { event.preventDefault(); close(); return true; }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); active = (active + (event.key === 'ArrowDown' ? 1 : -1) + entries.length) % entries.length;
        render(); return true;
      }
      if ((event.key === 'Enter' || event.key === 'Tab') && entries[active]) {
        event.preventDefault(); choose(entries[active]); return true;
      }
      return false;
    },
    receive: (id: number, files: WorkspaceEntry[]) => {
      if (id !== requestId || !mode || (mode === 'inline' && (!token || currentToken()?.query !== token.query))) return;
      if (deadline !== undefined) window.clearTimeout(deadline);
      entries = files; active = 0; loading = false; searchError = ''; render();
    },
    failed: (id: number, message: string) => {
      if (id !== requestId || !mode) return;
      if (deadline !== undefined) window.clearTimeout(deadline);
      entries = []; loading = false; searchError = message; render();
    },
    references,
    restore: (value: string, references: string[] = []): string => {
      selected.clear();
      let draft = value.trimEnd();
      for (const path of [...references].reverse()) {
        const label = mention(path);
        if (draft.endsWith(label)) draft = draft.slice(0, -label.length).trimEnd();
        selected.set(path, path.endsWith('/') ? 'directory' : 'file');
      }
      renderChips(); close(); return draft;
    },
    clear: () => { selected.clear(); error.hidden = true; close(); renderChips(); },
    refresh: () => { if (input.disabled) close(); renderChips(); },
  };
}
