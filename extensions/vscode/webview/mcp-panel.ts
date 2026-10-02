import type { ChatState } from '../src/session';
import type { PageMessage } from '../src/protocol';

export function installMcpPanel(input: HTMLTextAreaElement, post: (message: PageMessage) => void) {
  const panel = document.getElementById('mcp-panel')!;
  const list = document.getElementById('mcp-servers')!;
  const message = document.getElementById('mcp-message')!;
  const refresh = document.getElementById('mcp-refresh') as HTMLButtonElement;
  const apply = document.getElementById('mcp-apply') as HTMLButtonElement;
  const add = document.getElementById('mcp-add') as HTMLButtonElement;
  const configure = document.getElementById('mcp-configure') as HTMLButtonElement;
  const expanded = new Set<string>();
  let signature = '';
  let latest: ChatState | undefined;
  function text(tag: string, content: string, className = ''): HTMLElement {
    const node = document.createElement(tag); node.textContent = content; node.className = className; return node;
  }
  function action(label: string, key: string, callback: () => void): HTMLButtonElement {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
    button.dataset.focusKey = key; button.addEventListener('click', callback); return button;
  }
  function close(): void { panel.hidden = true; input.focus(); }
  document.getElementById('mcp-close')!.addEventListener('click', close);
  refresh.addEventListener('click', () => post({ type: 'refreshMcp' }));
  apply.addEventListener('click', () => post({ type: 'reloadMcp' }));
  add.addEventListener('click', () => post({ type: 'addMcpServer' }));
  configure.addEventListener('click', () => post({ type: 'openMcpSettings' }));
  panel.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
  });
  document.addEventListener('click', event => {
    if (!panel.hidden && !panel.contains(event.target as Node)) panel.hidden = true;
  });
  function render(state: ChatState): void {
    latest = state;
    const managing = !!state.mcpBusy;
    const locked = managing || state.connection !== 'ready' || state.busy || state.sending;
    refresh.disabled = managing || !!state.mcpLoading || state.connection !== 'ready';
    apply.disabled = locked; add.disabled = locked; configure.disabled = managing || state.connection !== 'ready';
    refresh.textContent = state.mcpLoading ? '刷新中…' : '刷新';
    const servers = state.mcpServers ?? [];
    const connected = servers.filter(server => server.status === 'connected');
    document.getElementById('mcp-summary')!.textContent = state.mcpServers === undefined
      ? '读取服务器状态…' : `${connected.length} / ${servers.length} 已连接 · ${connected.reduce((total, server) => total + server.tools.length, 0)} 个工具`;
    message.textContent = state.mcpError || state.mcpConfigError || (managing ? '正在更新 MCP 连接…'
      : state.mcpNeedsReload ? '配置已保存，点击“应用配置”更新连接。'
      : state.busy || state.sending ? '任务运行中，可查看状态；任务结束后可修改连接。' : '');
    message.hidden = !message.textContent;
    const nextSignature = JSON.stringify([servers, locked, state.mcpServers !== undefined]);
    if (signature === nextSignature) return;
    signature = nextSignature;
    const focusKey = (document.activeElement as HTMLElement)?.dataset.focusKey;
    list.replaceChildren();
    if (!servers.length) {
      list.append(text('div', state.mcpServers === undefined ? '正在读取 MCP 服务器…' : '尚未配置 MCP 服务器', 'mcp-empty'));
      return;
    }
    for (const server of servers) {
      const item = text('div', '', 'mcp-server');
      const row = text('div', '', 'mcp-server-row');
      const details = text('div', '', 'mcp-server-detail'); details.hidden = !expanded.has(server.name);
      const expand = action('', `expand:${server.name}`, () => {
        if (expanded.has(server.name)) expanded.delete(server.name); else expanded.add(server.name);
        expand.setAttribute('aria-expanded', String(expanded.has(server.name))); details.hidden = !expanded.has(server.name);
      });
      expand.className = 'mcp-expand'; expand.setAttribute('aria-expanded', String(expanded.has(server.name)));
      expand.setAttribute('aria-label', `${server.name} · 查看工具与连接详情`);
      expand.append(text('span', '›', 'mcp-chevron'), text('span', server.name, 'mcp-server-name'),
        text('span', server.transport === 'http' ? 'HTTP' : server.transport.toUpperCase(), 'mcp-transport'));
      const status = text('span', server.status === 'connected' ? `${server.tools.length} 个工具`
        : server.status === 'disabled' ? '已禁用' : '连接失败', 'mcp-server-status');
      status.dataset.state = server.status;
      const toggle = action(server.enabled ? '禁用' : '启用', `toggle:${server.name}`, () =>
        post({ type: 'setMcpEnabled', name: server.name, enabled: !server.enabled }));
      toggle.className = 'mcp-toggle'; toggle.disabled = locked;
      toggle.setAttribute('aria-label', `${server.enabled ? '禁用' : '启用'} ${server.name}`);
      row.append(expand, status, toggle); item.append(row, details);
      const address = server.transport === 'stdio' ? server.command || '' : server.transport === 'http'
        ? server.url || '' : `${server.host}:${server.port}`;
      details.append(text('div', address, 'mcp-address'));
      if (server.error) details.append(text('div', server.error, 'mcp-server-error'));
      const reconnect = action('重新连接', `retry:${server.name}`, () => post({ type: 'reconnectMcp', name: server.name }));
      reconnect.className = 'mcp-reconnect'; reconnect.disabled = locked || !server.enabled; details.append(reconnect);
      for (const tool of server.tools) {
        const definition = document.createElement('details'); definition.className = 'mcp-tool';
        definition.append(text('summary', tool.name), text('p', tool.description),
          text('pre', JSON.stringify(tool.inputSchema, null, 2)));
        details.append(definition);
      }
      if (!server.tools.length) details.append(text('div', server.status === 'connected'
        ? '服务器未提供工具' : '连接成功后显示可用工具', 'mcp-no-tools'));
      list.append(item);
    }
    if (focusKey) [...list.querySelectorAll<HTMLElement>('[data-focus-key]')]
      .find(node => node.dataset.focusKey === focusKey)?.focus();
  }
  function open(): void {
    panel.hidden = false;
    if (latest) render(latest);
    document.getElementById('mcp-close')!.focus(); post({ type: 'refreshMcp' });
  }
  return { open, render };
}
