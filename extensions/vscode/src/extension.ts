import * as vscode from 'vscode';
import { randomBytes } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { homedir } from 'node:os';
import { ensureCore } from './core';
import { ChatSession } from './session';
import { parsePageMessage } from './protocol';
import { webviewHtml } from './html';
import { BookmarkStore } from './bookmarks';
import { ArchiveStore } from './archives';
import { codeBlocks } from './code-blocks';

let provider: ChatProvider | undefined;

// 注册侧边栏和打开命令，在用户打开视图后才启动 core。
export function activate(context: vscode.ExtensionContext): void {
  provider = new ChatProvider(context);
  context.subscriptions.push(vscode.window.registerWebviewViewProvider('agentLite.chat', provider));
  context.subscriptions.push(vscode.commands.registerCommand('agentLite.open', () =>
    vscode.commands.executeCommand('workbench.view.extension.agentLite')));
  context.subscriptions.push(vscode.commands.registerCommand('agentLite.configureModels', () => provider?.openSettings()));
  context.subscriptions.push(vscode.commands.registerCommand('agentLite.configureMcp', () => provider?.openMcpSettings()));
  context.subscriptions.push(vscode.workspace.onDidSaveTextDocument(document => {
    if (document.uri.fsPath.endsWith('settings.json')) void provider?.refreshModels();
    provider?.mcpSettingsSaved(document.uri.fsPath);
  }));
}

// 卸载扩展时清理自己的任务和连接，保留共享 core。
export async function deactivate(): Promise<void> { await provider?.shutdown(); provider = undefined; }

class ChatProvider implements vscode.WebviewViewProvider {
  mcpSettingsSaved(path: string): void {
    if (path === this.session?.state.mcpSettingsPath) this.session.mcpSettingsSaved();
  }
  async refreshModels(): Promise<void> { await this.session?.refreshModels(); }
  private view?: vscode.WebviewView;
  private session?: ChatSession;
  private connecting = false;
  private disposed = false;
  private viewSubscriptions: vscode.Disposable[] = [];
  private readonly bookmarks: BookmarkStore;
  private readonly archives: ArchiveStore;

  // 保存插件资源和日志目录，不在页面中暴露进程控制能力。
  constructor(private readonly context: vscode.ExtensionContext) {
    this.bookmarks = new BookmarkStore(context.workspaceState);
    this.archives = new ArchiveStore(context.workspaceState);
  }

  // 创建受 CSP 限制的页面；页面就绪后发送宿主状态快照。
  resolveWebviewView(view: vscode.WebviewView): void {
    for (const subscription of this.viewSubscriptions) subscription.dispose();
    this.view = view;
    view.webview.options = { enableScripts: true, localResourceRoots: [
      vscode.Uri.joinPath(this.context.extensionUri, 'dist'),
      vscode.Uri.joinPath(this.context.extensionUri, 'media')
    ] };
    view.webview.html = this.html(view.webview);
    this.viewSubscriptions = [
      view.webview.onDidReceiveMessage(value => { void this.message(value); }),
      view.onDidDispose(() => { if (this.view === view) this.view = undefined; })
    ];
  }

  // 检查本地受信任工作区，避免在不受支持的环境启动进程。
  private supported(): boolean {
    if (!vscode.workspace.isTrusted || vscode.env.remoteName ||
        !vscode.workspace.workspaceFolders?.length ||
        vscode.workspace.workspaceFolders.some(folder => folder.uri.scheme !== 'file')) {
      this.post({ type: 'unavailable', message: '请打开受信任的本地文件夹。此 demo 暂不支持 Remote、WSL 或虚拟工作区。' });
      return false;
    }
    return true;
  }

  // 页面只能提交白名单操作，连接与权限状态仍由宿主验证。
  private async message(value: unknown): Promise<void> {
    const message = parsePageMessage(value);
    if (!message || this.disposed) return;
    if (message.type === 'ready') {
      if (this.session) this.post({ type: 'state', state: this.session.state });
      else await this.connect();
      return;
    }
    if (message.type === 'openLogs') { await this.openLogs(); return; }
    if (message.type === 'archiveSession' || message.type === 'restoreSession') {
      if (!this.session) return;
      try {
        await this.archives.set(this.session.state, message.sessionId, message.type === 'archiveSession');
        this.post({ type: 'state', state: this.session.state });
      } catch (error) { this.session.report(error); }
      return;
    }
    if (message.type === 'toggleBookmark' || message.type === 'removeBookmark') {
      if (!this.session) return;
      try {
        if (message.type === 'toggleBookmark') await this.bookmarks.toggle(this.session.state, message.cardId);
        else await this.bookmarks.remove(this.session.state.sessionId, message.bookmarkId);
        this.post({ type: 'state', state: this.session.state });
      } catch (error) { this.session.report(error); }
      finally { this.post({ type: 'bookmarksSettled' }); }
      return;
    }
    if (message.type === 'copyCode' || message.type === 'openCode') {
      const card = this.session?.state.cards.find(item => item.id === message.cardId && item.kind === 'assistant');
      const block = card && codeBlocks(card.text)[message.blockIndex];
      if (!block) return;
      try {
        if (message.type === 'copyCode') {
          await vscode.env.clipboard.writeText(block.text);
          vscode.window.setStatusBarMessage('代码已复制', 2000);
        } else {
          const aliases: Record<string, string> = { py: 'python', js: 'javascript', ts: 'typescript', ps1: 'powershell', sh: 'shellscript', bash: 'shellscript' };
          const language = aliases[block.language] ?? block.language;
          const languages = await vscode.languages.getLanguages();
          const document = await vscode.workspace.openTextDocument({ content: block.text, language: languages.includes(language) ? language : 'plaintext' });
          await vscode.window.showTextDocument(document, { preview: false });
        }
      } catch (error) { this.session?.report(error); }
      return;
    }
    if (message.type === 'copyAnswer' || message.type === 'openAnswer' || message.type === 'copyMessage') {
      const card = this.session?.state.cards.find(item => item.id === message.cardId &&
        (message.type === 'copyMessage' ? item.kind === 'user' : item.kind === 'assistant'));
      if (!card?.text.trim()) return;
      try {
        if (message.type !== 'openAnswer') {
          await vscode.env.clipboard.writeText(card.text);
          vscode.window.setStatusBarMessage('已复制', 2000);
          this.post({ type: 'copied', cardId: card.id });
        } else {
          const document = await vscode.workspace.openTextDocument({ content: card.text, language: 'markdown' });
          await vscode.window.showTextDocument(document, { preview: false });
        }
      } catch (error) { this.session?.report(error); }
      return;
    }
    if (!this.supported()) return;
    try {
      if (message.type === 'retry') {
        if (!this.session?.state.busy && !this.session?.state.sending) await this.connect();
      } else if (message.type === 'newSession') {
        if (this.session?.state.connection === 'ready') await this.session.newSession();
      } else if (message.type === 'send') await this.session?.send(message.content, message.images);
      else if (message.type === 'refreshHistory') await this.session?.refreshHistory();
      else if (message.type === 'refreshModels') await this.session?.refreshModels();
      else if (message.type === 'resumeSession') await this.session?.resumeSession(message.sessionId);
      else if (message.type === 'selectModel') await this.session?.selectModel(message.modelId);
      else if (message.type === 'openSettings') await this.openSettings();
      else if (message.type === 'refreshMcp') await this.session?.refreshMcp();
      else if (message.type === 'reloadMcp') await this.session?.manageMcp('reload');
      else if (message.type === 'reconnectMcp') await this.session?.manageMcp('reconnect', { name: message.name });
      else if (message.type === 'setMcpEnabled') await this.session?.manageMcp('set_enabled', { name: message.name, enabled: message.enabled });
      else if (message.type === 'openMcpSettings') await this.openMcpSettings();
      else if (message.type === 'addMcpServer') await this.addMcpServer();
      else if (message.type === 'renameSession') {
        const state = this.session?.state;
        const item = state?.history?.find(entry => entry.session_id === message.sessionId)
          ?? (state?.sessionId === message.sessionId ? { title: state.title || '新会话' } : undefined);
        if (!item || this.session?.state.busy || this.session?.state.sending) return;
        const title = await vscode.window.showInputBox({ title: '重命名会话', value: item.title,
          prompt: '为这次聊天起一个容易找到的名字',
          validateInput: value => !value.trim() ? '名称不能为空' : value.trim().length > 120 ? '最多 120 个字符' : undefined });
        if (title !== undefined) await this.session?.renameSession(message.sessionId, title.trim());
      }
      else if (message.type === 'cancel') await this.session?.cancel();
      else if (message.type === 'permission') await this.session?.permission(message.toolUseId, message.decision);
    } catch (error) {
      if (message.type === 'newSession') this.session?.fail(String(error));
      else this.session?.report(error);
    }
  }

  async openSettings(): Promise<void> {
    const directory = join(homedir(), '.agentlite');
    const path = join(directory, 'settings.json');
    await mkdir(directory, { recursive: true });
    try { await writeFile(path, JSON.stringify({ models: [] }, null, 2) + '\n', { flag: 'wx' }); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error; }
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(path));
    await vscode.window.showTextDocument(document, { preview: false });
  }

  async openMcpSettings(): Promise<void> {
    if (!this.session && this.supported()) await this.connect();
    const result = await this.session?.manageMcp('configure');
    if (!result || typeof result.settingsPath !== 'string' || !result.settingsPath) return;
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(result.settingsPath));
    await vscode.window.showTextDocument(document, { preview: false });
  }

  private async addMcpServer(): Promise<void> {
    const session = this.session;
    if (!session || session.state.busy || session.state.sending || session.state.mcpBusy) return;
    const name = await vscode.window.showInputBox({ title: '添加 MCP 服务器', prompt: '服务器名称',
      validateInput: value => !/^[A-Za-z0-9_-]{1,64}$/.test(value) ? '使用 1–64 位字母、数字、下划线或连字符'
        : session.state.mcpServers?.some(server => server.name === value) ? '名称已存在' : undefined });
    if (name === undefined) return;
    const transport = await vscode.window.showQuickPick([
      { label: 'STDIO', description: '启动本地 MCP 进程', value: 'stdio' },
      { label: 'Streamable HTTP', description: '连接 MCP URL', value: 'http' },
      { label: 'TCP', description: '兼容已有 AgentLite TCP 服务器', value: 'tcp' },
    ], { title: '添加 MCP 服务器', placeHolder: '选择连接方式' });
    if (!transport) return;
    const server: Record<string, unknown> = { name, transport: transport.value, enabled: true };
    if (transport.value === 'stdio') {
      const command = await vscode.window.showInputBox({ title: 'STDIO MCP', prompt: '可执行命令或完整路径',
        placeHolder: '例如 npx 或 python', validateInput: value => !value.trim() ? '命令不能为空' : undefined });
      if (command === undefined) return;
      const args = await vscode.window.showInputBox({ title: 'STDIO MCP', prompt: '命令参数（JSON 字符串数组）',
        value: '[]', placeHolder: '["-y", "@upstash/context7-mcp"]', validateInput: value => {
          try { const parsed = JSON.parse(value); if (Array.isArray(parsed) && parsed.every(arg => typeof arg === 'string')) return; }
          catch { /* 输入未完成时显示校验提示。 */ }
          return '请填写 JSON 字符串数组，如 ["server.py"]';
        } });
      if (args === undefined) return;
      server.command = command.trim(); server.args = JSON.parse(args);
    } else if (transport.value === 'http') {
      const url = await vscode.window.showInputBox({ title: 'HTTP MCP', prompt: 'MCP 服务器 URL',
        placeHolder: 'https://example.com/mcp', validateInput: value => {
          try { const url = new URL(value); if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password) return; }
          catch { /* 不接受不完整 URL。 */ }
          return '请填写有效的 http(s) URL';
        } });
      if (url === undefined) return;
      const token = await vscode.window.showInputBox({ title: 'HTTP MCP', prompt: 'Token 的环境变量名（可留空；在 ~/.agentlite/.env 中设置）',
        placeHolder: 'MY_MCP_TOKEN', validateInput: value => value && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(value)
          ? '填写环境变量名，不要填写 Token 本身' : undefined });
      if (token === undefined) return;
      server.url = url.trim(); server.bearer_token_env = token;
    } else {
      const host = await vscode.window.showInputBox({ title: 'TCP MCP', prompt: '服务器主机', value: 'localhost',
        validateInput: value => !value.trim() ? '主机不能为空' : undefined });
      if (host === undefined) return;
      const port = await vscode.window.showInputBox({ title: 'TCP MCP', prompt: '端口', value: '3000',
        validateInput: value => !/^\d+$/.test(value) || Number(value) < 1 || Number(value) > 65535 ? '端口应为 1–65535' : undefined });
      if (port === undefined) return;
      server.host = host.trim(); server.port = Number(port);
    }
    if (session === this.session) await session.manageMcp('add', { server });
  }

  // 将重连与首次启动串行化，确保一个窗口只管理一个会话。
  private async connect(): Promise<void> {
    if (this.connecting || this.disposed || !this.supported()) return;
    this.connecting = true;
    try {
      const folders = vscode.workspace.workspaceFolders!;
      const folder = folders.length === 1 ? folders[0] : await vscode.window.showWorkspaceFolderPick({
        placeHolder: '选择 AgentLite 本次使用的工作目录'
      });
      if (!folder || this.disposed) {
        if (!folder) this.post({ type: 'unavailable', message: '尚未选择工作目录，点击重试继续。' });
        return;
      }
      await this.session?.dispose();
      const session = new ChatSession(folder.uri.fsPath, state => this.post({ type: 'state', state }));
      this.session = session; session.beginConnection();
      const config = vscode.workspace.getConfiguration('agentLite');
      const port = config.get<number>('corePort', 7437);
      if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('agentLite.corePort 必须在 1–65535 之间');
      const connection = await ensureCore({ workspace: folder.uri.fsPath,
        pythonPath: config.get<string>('pythonPath', ''), port,
        coreDirectory: config.get<string>('coreDirectory', ''),
        storageDir: this.context.globalStorageUri.fsPath });
      if (this.disposed || !this.supported()) { connection.client.close(); return; }
      await session.attach(connection.client, connection.origin);
    } catch (error) { this.session?.fail(error instanceof Error ? error.message : String(error)); }
    finally { this.connecting = false; }
  }

  // 发送页面快照，隐藏的页面由宿主继续保留状态。
  private post(message: unknown): void {
    if (this.disposed) return;
    if (message && typeof message === 'object' && 'type' in message && message.type === 'state' && 'state' in message) {
      const state = message.state as import('./session').ChatState;
      message = { type: 'state', state: { ...state, archivedSessionIds: this.archives.list(),
        bookmarks: this.bookmarks.list(state.sessionId), bookmarkCardIds: this.bookmarks.matches(state) } };
    }
    void this.view?.webview.postMessage(message);
  }

  // 通过编辑器打开诊断文件；自定义 core 日志路径由用户配置决定。
  private async openLogs(): Promise<void> {
    const files = [join(this.context.globalStorageUri.fsPath, 'core-launch.log'),
      join(homedir(), '.agentlite', 'logs', 'core.log')].filter(existsSync);
    if (!files.length) { void vscode.window.showInformationMessage('尚无日志。自定义日志路径请查看 AgentLite 配置。'); return; }
    const selected = files.length === 1 ? files[0] : await vscode.window.showQuickPick(files, { placeHolder: '选择诊断日志' });
    if (selected) await vscode.window.showTextDocument(vscode.Uri.file(selected));
  }

  // 使用本地打包资源与随机 nonce，不允许内联事件或外部脚本。
  private html(webview: vscode.Webview): string {
    const nonce = randomBytes(16).toString('hex');
    const script = webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, 'dist', 'webview.js'));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, 'media', 'chat.css'));
    const logo = webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, 'media', 'logo.svg'));
    return webviewHtml(nonce, webview.cspSource, script.toString(), style.toString(), logo.toString());
  }

  // 终止宿主订阅并等待自己的会话清理。
  async shutdown(): Promise<void> {
    this.disposed = true;
    for (const subscription of this.viewSubscriptions) subscription.dispose();
    await this.session?.dispose();
  }
}
