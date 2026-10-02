import * as vscode from 'vscode';
import assert from 'node:assert/strict';
import { server, reply, waitFor } from './helpers';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { existsSync } from 'node:fs';

// 在真正的扩展宿主加载插件，验证 Webview 脚本就绪后完成会话订阅。
export async function run(): Promise<void> {
  const methods: string[] = [];
  const fixture = await server((socket, message) => {
    methods.push(message.method);
    if (message.method === 'core.ping') reply(socket, message.id, { server_version: '0.0.1' });
    else if (message.method === 'session.create') reply(socket, message.id, { session_id: 'vscode-smoke' });
    else reply(socket, message.id, {});
  });
  try {
    await vscode.workspace.getConfiguration('agentLite').update('corePort', fixture.port, vscode.ConfigurationTarget.Global);
    const extension = vscode.extensions.getExtension('agentlite-local.agentlite-vscode');
    assert(extension, '未注册开发扩展'); await extension.activate();
    await vscode.commands.executeCommand('agentLite.open');
    await vscode.commands.executeCommand('agentLite.chat.focus');
    await waitFor(() => methods.includes('event.subscribe'), 15_000);
    assert.deepEqual(methods.slice(0, 4), ['core.ping', 'frontend.register', 'session.create', 'event.subscribe']);
    await vscode.commands.executeCommand('agentLite.configureModels');
    assert.equal(vscode.window.activeTextEditor?.document.uri.fsPath,
      vscode.Uri.file(join(homedir(), '.agentlite', 'settings.json')).fsPath);
    assert(!existsSync(join(vscode.workspace.workspaceFolders![0].uri.fsPath, '.agentlite')));
    console.log('VSCODE_SMOKE_OK: actual webview ready, session subscribed, model configuration opens user directory');
  } finally { await fixture.close(); }
}
