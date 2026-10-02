type Command = { id: string; label: string; description: string; shape: string; pending?: boolean };
const commands: Command[] = [
  { id: 'new', label: '新建会话', description: '开始一段新的聊天', shape: '<path d="M12 4v16M4 12h16"/>' },
  { id: 'history', label: '历史会话', description: '搜索和继续之前的聊天', shape: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>' },
  { id: 'model', label: '模型', description: '切换当前使用的模型', shape: '<path d="m12 3 9 5v8l-9 5-9-5V8zM3 8l9 5 9-5M12 13v8"/>' },
  { id: 'settings', label: '模型配置', description: '打开用户模型配置', shape: '<circle cx="12" cy="12" r="4"/><path d="M12 2v4m0 12v4M2 12h4m12 0h4M5 5l3 3m8 8 3 3M5 19l3-3m8-8 3-3"/>' },
  { id: 'status', label: '状态', description: '查看会话、模型和用量', shape: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>' },
  { id: 'logs', label: '日志', description: '打开 AgentLite 运行日志', shape: '<path d="M6 3h12v18H6zM9 7h6M9 12h6M9 17h4"/>' },
  { id: 'goal', label: '目标', description: '设置持续推进的任务目标', pending: true, shape: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/>' },
  { id: 'plan', label: '计划模式', description: '先制定计划，再执行任务', pending: true, shape: '<path d="M9 18h6m-5 3h4M8 14a6 6 0 1 1 8 0l-1 4H9z"/>' },
  { id: 'reasoning', label: '推理强度', description: '调整模型的推理深度', pending: true, shape: '<path d="M12 3v18M12 5C5 0 2 8 5 11c-5 4 1 12 7 8M12 5c7-5 10 3 7 6 5 4-1 12-7 8"/>' },
  { id: 'mcp', label: 'MCP', description: '查看和管理 MCP 服务', shape: '<path d="m8 8 8 8m-9-5 6-6m-8 8 6-6m1 10 6-6"/>' },
  { id: 'memories', label: '记忆', description: '管理长期偏好与项目记忆', pending: true, shape: '<path d="M12 3v18M12 5C5 0 2 8 5 11c-5 4 1 12 7 8M12 5c7-5 10 3 7 6 5 4-1 12-7 8"/>' },
  { id: 'skills', label: '技能', description: '浏览可用技能', pending: true, shape: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z"/>' },
];

// 命令注册表与输入交互独立于宿主协议，未实现的入口不会提交模型任务。
export function installSlashCommands(input: HTMLTextAreaElement, execute: (id: string) => void) {
  const menu = document.getElementById('slash-menu')!;
  const options = document.getElementById('slash-options')!;
  const detail = document.getElementById('command-detail')!;
  const title = document.getElementById('command-title')!;
  const body = document.getElementById('command-body')!;
  let matches: Command[] = [];
  let selected = 0;
  let composing = false;
  function close(): void {
    menu.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  }
  function showDetail(heading: string, content: string): void {
    close(); title.textContent = heading; body.textContent = content; detail.hidden = false;
    document.getElementById('command-close')!.focus();
  }
  function choose(command: Command): void {
    if (input.disabled) return;
    close(); detail.hidden = true; input.value = ''; input.dispatchEvent(new Event('input')); input.focus();
    if (command.pending) showDetail(command.label, `${command.description}。此功能尚未实现，已保留入口，后续接入。`);
    else execute(command.id);
  }
  function highlight(): void {
    const rows = [...options.querySelectorAll<HTMLElement>('[role="option"]')];
    rows.forEach((row, index) => row.setAttribute('aria-selected', String(index === selected)));
    const active = rows[selected];
    if (active) { input.setAttribute('aria-activedescendant', active.id); active.scrollIntoView?.({ block: 'nearest' }); }
    else input.removeAttribute('aria-activedescendant');
  }
  function update(): void {
    const match = /^\s*\/([^\s/]*)$/.exec(input.value);
    if (input.disabled || composing || !match || input.selectionStart !== input.value.length || input.selectionEnd !== input.value.length) { close(); return; }
    const query = match[1].toLocaleLowerCase();
    matches = commands.filter(command => `${command.id} ${command.label} ${command.description}`.toLocaleLowerCase().includes(query));
    selected = 0; options.replaceChildren(); detail.hidden = true;
    for (const command of matches) {
      const row = document.createElement('button'); row.type = 'button'; row.className = 'slash-option';
      row.id = `slash-${command.id}`; row.setAttribute('role', 'option'); row.tabIndex = -1;
      row.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${command.shape}</svg><span class="slash-copy"><span class="slash-label">${command.label}<span class="slash-name">/${command.id}</span></span></span>${command.pending ? '<span class="slash-badge">待实现</span>' : ''}`;
      row.addEventListener('mousedown', event => event.preventDefault());
      row.addEventListener('mouseenter', () => { selected = matches.indexOf(command); highlight(); });
      row.addEventListener('click', event => { event.stopPropagation(); choose(command); }); options.append(row);
    }
    if (!matches.length) { const empty = document.createElement('div'); empty.className = 'slash-empty'; empty.textContent = '没有匹配的命令'; options.append(empty); }
    menu.hidden = false; input.setAttribute('aria-expanded', 'true'); highlight();
  }
  function handleKey(event: KeyboardEvent): boolean {
    if (event.isComposing || composing || menu.hidden) return false;
    if (event.key === 'Escape' || event.key === 'Tab' && !matches.length) { close(); if (event.key === 'Tab') return false; }
    else if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      if (matches.length) { selected = (selected + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length; highlight(); }
    } else if (['Enter', 'Tab'].includes(event.key) && !event.shiftKey && matches.length) choose(matches[selected]);
    else return false;
    event.preventDefault(); event.stopPropagation(); return true;
  }
  // 点击发送与键盘确认使用相同分发路径；Esc 关闭后输入完整命令仍可执行。
  function submit(): boolean {
    if (input.disabled || composing) return false;
    const command = commands.find(item => input.value.trim().toLowerCase() === `/${item.id}`);
    if (command) { choose(command); return true; }
    if (!menu.hidden && matches.length) { choose(matches[selected]); return true; }
    return false;
  }
  input.addEventListener('input', update);
  input.addEventListener('click', update);
  input.addEventListener('keyup', event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) update(); });
  input.addEventListener('compositionstart', () => { composing = true; close(); });
  input.addEventListener('compositionend', () => { composing = false; update(); });
  input.addEventListener('blur', close);
  document.addEventListener('click', event => {
    if (!menu.contains(event.target as Node) && event.target !== input) close();
    if (!detail.contains(event.target as Node)) detail.hidden = true;
  });
  function dismissDetail(): void { detail.hidden = true; input.focus(); }
  document.getElementById('command-close')!.addEventListener('click', dismissDetail);
  detail.addEventListener('keydown', event => { if (event.key === 'Escape') { event.stopPropagation(); dismissDetail(); } });
  return { close, handleKey, submit, showDetail };
}
