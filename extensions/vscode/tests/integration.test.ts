import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import { RpcClient } from '../src/rpc';
import { ChatSession } from '../src/session';
import { freePort, waitFor } from './helpers';

const repository = resolve('../..');
const python = process.env.AGENTLITE_TEST_PYTHON || join(repository, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');

// 功能：TypeScript 客户端通过真实 Python core 完成读文件、追问、权限、超时和取消。
// 设计：只替换 LLM 和全局策略存储，保留 TCP、CoreApp、AgentRunner、SessionManager 与真实文件工具。
test('real Python core: multi-turn tools, approval, timeout, cancel, shared lifetime', { skip: !existsSync(python), timeout: 60_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), 'agentlite-vscode-core-')); const port = await freePort();
  writeFileSync(join(root, 'sample.txt'), '真实文件内容 7391', 'utf8');
  const child = spawn(python, [resolve('tests/fixtures/core_stub.py')], {
    cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONPATH: join(repository, 'src'), PYTHONIOENCODING: 'utf-8',
      AGENTLITE_PORT: String(port), AGENTLITE_TEST_DIR: root }
  });
  let diagnostics = ''; child.stderr.on('data', chunk => { diagnostics += chunk.toString(); });
  child.on('error', error => { diagnostics += error.message; });
  const session = new ChatSession(root, () => {}); let client: RpcClient | undefined;
  try {
    await waitFor(() => child.exitCode === null, 100);
    const deadline = Date.now() + 30_000;
    while (!client && Date.now() < deadline) {
      if (child.exitCode !== null || child.signalCode !== null) throw new Error(diagnostics);
      try { client = await RpcClient.connect(port, 300); }
      catch { await new Promise(resolve => setTimeout(resolve, 100)); }
    }
    assert(client, diagnostics); await session.attach(client, 'started');
    await session.send('read sample');
    assert(session.state.cards.some(card => card.kind === 'tool' && card.output?.includes('7391')));
    await session.send('follow up');
    assert.equal(session.state.cards.filter(card => card.kind === 'user').length, 2);
    const approved = session.send('permission write');
    await waitFor(() => session.state.cards.some(card => card.kind === 'permission' && card.status === 'pending'));
    const permission = session.state.cards.findLast(card => card.kind === 'permission')!;
    await session.permission(permission.toolUseId!, 'allow_once'); await approved;
    assert.equal(readFileSync(join(root, 'result.txt'), 'utf8'), '批准后的内容');
    const expired = session.send('permission timeout'); await expired;
    assert.equal(session.state.cards.findLast(card => card.kind === 'permission')?.status, 'timeout');
    const cancelled = session.send('cancel long task');
    await waitFor(() => session.state.cards.some(card => card.kind === 'assistant' && card.text === '等待取消'));
    await session.cancel(); await cancelled;
    assert(!session.state.busy); assert(session.state.cards.some(card => card.text === '已停止'));
    await session.send('after cancellation');
    assert.equal(session.state.connection, 'ready');
    await session.dispose();
    const second = await RpcClient.connect(port);
    assert((await second.request('core.ping', { client: 'lifetime-test' })).server_version);
    await second.request('core.shutdown', {}); second.close();
    await waitFor(() => child.exitCode !== null || child.signalCode !== null);
  } finally {
    await session.dispose(); client?.close();
    if (child.exitCode === null && child.signalCode === null) {
      child.kill(); await waitFor(() => child.exitCode !== null || child.signalCode !== null).catch(() => {});
    }
    rmSync(root, { recursive: true, force: true });
  }
});

// 功能：真实自动启动使用独立后台进程，启动宿主退出后仍可复用 core。
// 设计：单独 Node 子进程执行生产 ensureCore，使用本地假凭证仅测试握手，随后显式关闭测试拥有的 core。
test('real auto-start survives launcher exit and stops after all frontends leave', { skip: !existsSync(python), timeout: 90_000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), 'agentlite-detached-')); const port = await freePort();
  const configPath = join(root, 'config.toml');
  const foreignWorkspace = join(root, 'foreign-project'); mkdirSync(foreignWorkspace);
  // 目标项目的配置故意无效，证明启动只读取独立 core 配置目录。
  writeFileSync(join(foreignWorkspace, '.env'), 'LLM_PROTOCOL=invalid-project-protocol\n');
  const pathValue = (path: string) => JSON.stringify(path.replace(/\\/g, '/'));
  writeFileSync(configPath, `[llm]\nprotocol = "openai"\ndefault_model = "local-test"\nbase_url = "http://127.0.0.1:1"\n` +
    `[trace]\nenabled = false\n[web]\nenabled = false\n[logging]\nfile = ${pathValue(join(root, 'core.log'))}\n` +
    `[session]\ndir = ${pathValue(join(root, 'sessions'))}\n[memory]\ndir = ${pathValue(join(root, 'memory.db'))}\ngenerate_enabled = false\n`);
  const launcher = spawn(process.execPath, ['--import', 'tsx', resolve('tests/fixtures/launch_core.ts'), foreignWorkspace, python, String(port), root, root], {
    windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, AGENTLITE_CONFIG: configPath, LLM_API_KEY: 'local-test-only' }
  });
  let output = ''; let error = '';
  launcher.stdout.on('data', chunk => { output += chunk.toString(); });
  launcher.stderr.on('data', chunk => { error += chunk.toString(); });
  const competitors = [0, 1].map(() => {
    const child = spawn(process.execPath, ['--import', 'tsx', resolve('tests/fixtures/launch_core.ts'), foreignWorkspace, python, String(port), root, root], {
      windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, AGENTLITE_CONFIG: configPath, LLM_API_KEY: 'local-test-only' }
    });
    const result = { child, output: '', error: '' };
    child.stdout.on('data', chunk => { result.output += chunk.toString(); });
    child.stderr.on('data', chunk => { result.error += chunk.toString(); });
    return result;
  });
  let client: RpcClient | undefined;
  try {
    await waitFor(() => [launcher, ...competitors.map(item => item.child)].every(child => child.exitCode !== null || child.signalCode !== null), 50_000);
    assert.equal(launcher.exitCode, 0, error);
    for (const item of competitors) assert.equal(item.child.exitCode, 0, item.error);
    assert.deepEqual([output, ...competitors.map(item => item.output)].sort(), ['reused', 'reused', 'started']);
    assert.equal(readFileSync(join(root, 'core-launch.log'), 'utf8').match(/AgentLite background pid=/g)?.length, 1);
    client = await RpcClient.connect(port);
    assert((await client.request('core.ping', { client: 'after-launcher-exit' })).server_version);
    if (process.platform === 'win32') {
      // 功能：后台 core 没有可见控制台，启动器退出后仍独立运行。
      // 设计：对生产启动日志中的 PID 调用 Windows AttachConsole，真实验证系统进程属性。
      const pid = readFileSync(join(root, 'core-launch.log'), 'utf8').match(/AgentLite background pid=(\d+)/)?.[1];
      assert(pid, 'background launcher must record its PID');
      const check = 'import ctypes, sys; k = ctypes.WinDLL("kernel32", use_last_error=True); u = ctypes.WinDLL("user32"); k.GetConsoleWindow.restype = ctypes.c_void_p; u.IsWindowVisible.argtypes = [ctypes.c_void_p]; k.FreeConsole(); attached = k.AttachConsole(int(sys.argv[1])); error = ctypes.get_last_error(); window = k.GetConsoleWindow() if attached else None; visible = bool(u.IsWindowVisible(window)) if window else False; k.FreeConsole(); assert (attached or error == 6) and not visible, (attached, error, window, visible)';
      execFileSync(python, ['-c', check, pid], { windowsHide: true, timeout: 5000 });
    }
    const lease = await client.request('frontend.register', { client: 'vscode' });
    assert.equal(lease.managed, true);
    const created = await client.request('session.create', { mode: 'chat', workspace_root: foreignWorkspace });
    assert.equal(created.workspace_root, foreignWorkspace);
    await client.request('session.close', { session_id: created.session_id });
    const tui = await RpcClient.connect(port);
    try {
      await tui.request('frontend.register', { client: 'tui' });
      await client.request('frontend.unregister', {});
      client.close(); client = undefined;
      await tui.request('frontend.heartbeat', {});
      assert((await tui.request('core.ping', { client: 'remaining-tui' })).server_version);
    } finally { tui.close(); }
    // 普通查询连接保持打开也不能延长租约，最后一个前端断线后正常自动退出。
    client = await RpcClient.connect(port);
    await waitFor(async () => {
      try { await client!.request('core.ping', { client: 'observer' }, 500); return false; }
      catch { return true; }
    }, 25_000);
  } finally {
    for (const item of competitors) {
      if (item.child.exitCode === null && item.child.signalCode === null) {
        item.child.kill();
        await waitFor(() => item.child.exitCode !== null || item.child.signalCode !== null).catch(() => {});
      }
    }
    if (launcher.exitCode === null && launcher.signalCode === null) {
      launcher.kill(); await waitFor(() => launcher.signalCode !== null || launcher.exitCode !== null).catch(() => {});
    }
    try {
      client ??= await RpcClient.connect(port, 500);
      await client.request('core.shutdown', {}, 1500);
    } catch { /* 启动失败时没有测试 core 可关闭。 */ }
    client?.close();
    // 等待 Python 释放日志与 SQLite 文件，再清理已验证属于本测试的临时目录。
    await waitFor(() => {
      try { rmSync(root, { recursive: true, force: true }); return true; } catch { return false; }
    }).catch(() => {});
  }
});
