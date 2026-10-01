import * as vscode from 'vscode';
import assert from 'node:assert/strict';
import { server, reply, waitFor } from './helpers';

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
    console.log('VSCODE_SMOKE_OK: extension activated, actual webview ready, session subscribed');
  } finally { await fixture.close(); }
}
