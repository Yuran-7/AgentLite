import * as vscode from 'vscode';
import { randomBytes } from 'node:crypto';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import { ensureCore } from './core';
import { ChatSession } from './session';
import { parsePageMessage } from './protocol';
import { webviewHtml } from './html';

let provider: ChatProvider | undefined;

// 注册侧边栏和打开命令，在用户打开视图后才启动 core。
export function activate(context: vscode.ExtensionContext): void {
  provider = new ChatProvider(context);
  context.subscriptions.push(vscode.window.registerWebviewViewProvider('agentLite.chat', provider));
  context.subscriptions.push(vscode.commands.registerCommand('agentLite.open', () =>
    vscode.commands.executeCommand('workbench.view.extension.agentLite')));
}

// 卸载扩展时清理自己的任务和连接，保留共享 core。
export async function deactivate(): Promise<void> { await provider?.shutdown(); provider = undefined; }

class ChatProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private session?: ChatSession;
  private connecting = false;
  private disposed = false;
  private viewSubscriptions: vscode.Disposable[] = [];

  // 保存插件资源和日志目录，不在页面中暴露进程控制能力。
  constructor(private readonly context: vscode.ExtensionContext) {}

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
    if (!this.supported()) return;
    try {
      if (message.type === 'retry') {
        if (!this.session?.state.busy && !this.session?.state.sending) await this.connect();
      } else if (message.type === 'newSession') {
        if (this.session?.state.connection === 'ready') await this.session.newSession();
      } else if (message.type === 'send') await this.session?.send(message.content);
      else if (message.type === 'cancel') await this.session?.cancel();
      else if (message.type === 'permission') await this.session?.permission(message.toolUseId, message.decision);
    } catch (error) {
      if (message.type === 'newSession') this.session?.fail(String(error));
      else this.session?.report(error);
    }
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
      this.session = session; session.connecting();
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
  private post(message: unknown): void { if (!this.disposed) void this.view?.webview.postMessage(message); }

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
    return webviewHtml(nonce, webview.cspSource, script.toString(), style.toString());
  }

  // 终止宿主订阅并等待自己的会话清理。
  async shutdown(): Promise<void> {
    this.disposed = true;
    for (const subscription of this.viewSubscriptions) subscription.dispose();
    await this.session?.dispose();
  }
}
