import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import { RpcClient } from '../src/rpc';
import { ChatSession } from '../src/session';
import { freePort, waitFor } from './helpers';

// 功能：显式运行时使用项目配置的真实模型验收读文件、追问、写入审批与取消。
// 设计：所有工具工作区与会话均在临时目录，只允许写验收文件，不加载用户 MCP 或权限缓存。
test('live configured model: read, follow up, approve write, cancel and continue', { timeout: 180_000 }, async () => {
  const repository = resolve('../..');
  const python = process.env.AGENTLITE_TEST_PYTHON || join(repository, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  assert(existsSync(python), '需要已安装 AgentLite 的 Python 环境');
  const root = mkdtempSync(join(tmpdir(), 'agentlite-live-')); const port = await freePort();
  writeFileSync(join(root, 'sample.txt'), 'AgentLite demo magic number: 7391\n', 'utf8');
  const child = spawn(python, [resolve('tests/fixtures/live_core.py')], {
    cwd: repository, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'],
    env: { ...process.env, AGENTLITE_TEST_DIR: root, AGENTLITE_PORT: String(port), PYTHONIOENCODING: 'utf-8' }
  });
  // 错误诊断留在临时日志，不输出可能包含配置细节的完整 stderr。
  child.stderr.resume();
  const session = new ChatSession(root, () => {}); let client: RpcClient | undefined;
  try {
    const deadline = Date.now() + 30_000;
    while (!client && Date.now() < deadline) {
      if (child.exitCode !== null) throw new Error('真实模型 core 启动失败，请检查模型配置');
      try { client = await RpcClient.connect(port, 300); }
      catch { await new Promise(resolve => setTimeout(resolve, 100)); }
    }
    assert(client, '真实模型 core 启动超时'); await session.attach(client, 'started');
    await session.send('请只使用 read_file 读取当前工作区 sample.txt，并简短回复其中的 magic number。不要执行其他工具。');
    assert(session.state.cards.some(card => card.kind === 'tool' && card.title === 'read_file' && card.output?.includes('7391')));
    console.log('LIVE_READ_OK');
    await session.send('刚才读到的 magic number 是多少？直接回答，不要调用工具。');
    assert(session.state.cards.findLast(card => card.kind === 'assistant')?.text.includes('7391'));
    console.log('LIVE_FOLLOWUP_OK');
    const write = session.send('请只调用 write_file，将当前工作区 demo-result.txt 的内容写为 AgentLite demo approved。不要使用 shell，不要修改其他文件。');
    await waitFor(() => session.state.cards.some(card => card.kind === 'permission' && card.status === 'pending') || !session.state.busy, 60_000);
    const permission = session.state.cards.findLast(card => card.kind === 'permission' && card.status === 'pending');
    assert(permission, session.state.error ?? '没有收到 write_file 权限请求');
    assert.equal(permission.title, 'write_file', '只批准本次验收的 write_file');
    const params = permission.params as { path: string };
    assert.equal(resolve(root, params.path), join(root, 'demo-result.txt'), '只批准临时工作区内的验收文件');
    await session.permission(permission.toolUseId!, 'allow_once'); await write;
    assert(readFileSync(join(root, 'demo-result.txt'), 'utf8').includes('AgentLite demo approved'));
    console.log('LIVE_PERMISSION_OK');
    const cancelled = session.send('请详细解释 sample.txt 内容，至少写 1000 字。');
    await waitFor(() => !!session.state.runId, 5000); await session.cancel(); await cancelled;
    assert(session.state.cards.some(card => card.text === '已停止')); assert(!session.state.busy);
    await session.send('只回复 OK，不要调用任何工具。');
    assert(session.state.cards.findLast(card => card.kind === 'assistant')?.text.includes('OK'));
    console.log('LIVE_CANCEL_AND_CONTINUE_OK');
  } finally {
    await session.dispose();
    try {
      const cleanup = await RpcClient.connect(port, 500); await cleanup.request('core.shutdown', {}, 1500); cleanup.close();
      await waitFor(() => child.exitCode !== null || child.signalCode !== null, 5000);
    } catch { if (child.exitCode === null && child.signalCode === null) child.kill(); }
    client?.close();
    await waitFor(() => {
      try { rmSync(root, { recursive: true, force: true }); return true; } catch { return false; }
    }).catch(() => {});
  }
});
