import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { ensureCore, selectPython, runtime, type CoreOptions } from '../src/core';
import { server, freePort, reply } from './helpers';
import type { Socket } from 'node:net';

const options = (port: number): CoreOptions => ({ port, workspace: process.cwd(), pythonPath: '', storageDir: tmpdir() });
// 为启动测试提供能通过握手的 core 替身。
const ping = (socket: Socket, message: any) => reply(socket, message.id, { server_version: '0.0.1' });

// 功能：已有 core 无需解释器检查，客户端关闭后服务继续存在。
// 设计：让启动依赖直接抛错，确认复用路径完全不触发启动，再建立第二条连接。
test('reuse core without launching; closing client leaves core alive', async () => {
  const fixture = await server(ping);
  try {
    const forbidden = async () => { throw new Error('must not launch'); };
    const first = await ensureCore(options(fixture.port), { preflight: forbidden, launch: forbidden });
    assert.equal(first.origin, 'reused'); first.client.close();
    const second = await ensureCore(options(fixture.port)); second.client.close();
    assert.equal(second.origin, 'reused');
  } finally { await fixture.close(); }
});

// 功能：两个窗口同时连接空闲端口，只有唯一启动者执行 Python 预检。
// 设计：并发调用真实启动锁，延迟预检放大竞争窗口，断言另一连接直接复用。
test('startup and concurrent port race connect to surviving core', async () => {
  const port = await freePort(); let fixture: Awaited<ReturnType<typeof server>> | undefined;
  let checked = 0;
  let launched = false;
  const dependencies = {
    preflight: async () => { checked++; await new Promise(resolve => setTimeout(resolve, 100)); },
    launch: async () => {
      if (launched) return;
      launched = true;
      const { createServer } = await import('node:net');
      const listener = createServer(socket => socket.on('data', chunk => {
        const message = JSON.parse(chunk.toString()); ping(socket, message);
      }));
      await new Promise<void>(resolve => listener.listen(port, '127.0.0.1', resolve));
      fixture = { port, listener, close: () => new Promise<void>(resolve => listener.close(() => resolve())) };
    }
  };
  try {
    const connections = await Promise.all([ensureCore(options(port), dependencies), ensureCore(options(port), dependencies)]);
    assert.equal(checked, 1);
    assert.deepEqual(connections.map(item => item.origin).sort(), ['reused', 'started']);
    connections.forEach(item => item.client.close());
  } finally { await fixture?.close(); }
});

// 功能：占用端口却无效的服务不得触发后台进程启动。
// 设计：服务返回合法 RPC 但缺少版本字段，检验握手而不是仅检查 TCP 可达。
test('invalid service reports port conflict without launching', async () => {
  const fixture = await server((socket, message) => reply(socket, message.id, { wrong: true }));
  let launched = false;
  try {
    await assert.rejects(ensureCore(options(fixture.port), {
      preflight: async () => { launched = true; }, launch: async () => { launched = true; }
    }), /core.ping/);
    assert.equal(launched, false);
  } finally { await fixture.close(); }
});

// 功能：解释器不可用或服务未就绪时提供可行动的错误。
// 设计：分别走真实预检与缩短启动期限的无监听服务，不依赖机器 Python 配置。
test('interpreter preflight and startup timeout fail explicitly', async () => {
  await assert.rejects(runtime.preflight('agentlite-nonexistent-python', process.cwd()), /Python 3.12/);
  const port = await freePort();
  await assert.rejects(ensureCore(options(port), { preflight: async () => {}, launch: async () => {} }, 50), /未在/);
});

// 功能：验证 Python 配置覆盖本地虚拟环境与 PATH。
// 设计：创建临时环境路径并模拟 Windows 与 POSIX，避免依赖开发机实际 .venv。
test('Python selection follows configured, workspace venv, PATH order', () => {
  const root = mkdtempSync(join(tmpdir(), 'agentlite-python-'));
  try {
    assert.equal(selectPython(root, '', 'win32'), 'python');
    mkdirSync(join(root, '.venv', 'Scripts'), { recursive: true });
    const executable = join(root, '.venv', 'Scripts', 'python.exe'); writeFileSync(executable, '');
    assert.equal(selectPython(root, '', 'win32'), executable);
    assert.equal(selectPython(root, 'custom-python', 'win32'), 'custom-python');
    assert.equal(selectPython(root, '', 'linux'), 'python');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

// 功能：无效项目环境会回退到可用 Python，并跨项目复用缓存与独立 core 目录。
// 设计：让旧缓存和项目虚拟环境预检失败，观察实际候选顺序、启动参数及下一项目的选择。
test('runtime discovery skips invalid project venv and caches Python across projects', async () => {
  const root = mkdtempSync(join(tmpdir(), 'agentlite-runtime-'));
  const workspace = join(root, 'project'); mkdirSync(workspace);
  const venv = join(workspace, '.venv', process.platform === 'win32' ? 'Scripts' : 'bin'); mkdirSync(venv, { recursive: true });
  writeFileSync(join(venv, process.platform === 'win32' ? 'python.exe' : 'python'), '');
  const storageDir = join(root, 'storage'); mkdirSync(storageDir);
  writeFileSync(join(storageDir, 'runtime.json'), JSON.stringify({ pythonPath: 'missing-cache' }));
  const checked: string[] = []; let launched: CoreOptions | undefined;
  const dependencies = {
    preflight: async (python: string) => {
      checked.push(python);
      if (python !== 'python' && python !== 'shared-python') throw new Error('not installed');
      return { pythonPath: 'shared-python', coreDirectory: join(root, 'core-config') };
    },
    launch: async (python: string, args: CoreOptions) => { assert.equal(python, 'shared-python'); launched = args; }
  };
  try {
    await assert.rejects(ensureCore({ ...options(await freePort()), workspace, storageDir }, dependencies, 50), /未在/);
    assert.deepEqual(checked, ['missing-cache', selectPython(workspace, ''), 'python']);
    assert.equal(launched!.workspace, workspace); assert.equal(launched!.coreDirectory, join(root, 'core-config'));
    assert.equal(JSON.parse(readFileSync(join(storageDir, 'runtime.json'), 'utf8')).pythonPath, 'shared-python');
    checked.length = 0;
    await assert.rejects(ensureCore({ ...options(await freePort()), workspace: root, storageDir }, dependencies, 50), /未在/);
    assert.deepEqual(checked, ['shared-python']);
    checked.length = 0;
    await assert.rejects(ensureCore({ ...options(await freePort()), workspace, storageDir, pythonPath: 'explicit-bad' }, dependencies, 1), /not installed/);
    assert.deepEqual(checked, ['explicit-bad']);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

// 功能：实际 Python 预检可以从另一个项目发现 AgentLite 安装目录。
// 设计：使用真实 editable Python 环境，同时给目标项目放置无效配置，验证不会加载目标项目 .env。
test('real Python discovers core source independent of opened workspace', { skip: !existsSync(resolve('../../.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')) }, async () => {
  const root = mkdtempSync(join(tmpdir(), 'agentlite-foreign-'));
  try {
    writeFileSync(join(root, '.env'), 'LLM_PROTOCOL=invalid-project-protocol\n');
    const info = await runtime.preflight(resolve('../../.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'), root);
    assert(info); assert.equal(info.coreDirectory, resolve('../..'));
    assert(info.pythonPath);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

// 功能：core 缺少凭证并退出时立即报告原因，而不等待完整 30 秒。
// 设计：启动真实短命进程返回其 PID，并验证错误只提取安全的凭证变量名。
test('early startup exit surfaces missing credentials without leaking logs', async () => {
  const root = mkdtempSync(join(tmpdir(), 'agentlite-exit-'));
  try {
    await assert.rejects(ensureCore({ ...options(await freePort()), storageDir: root }, {
      preflight: async () => {},
      launch: async () => {
        const child = spawn(process.execPath, ['-e', ''], { windowsHide: true, stdio: 'ignore' });
        const pid = child.pid!; await new Promise(resolve => child.once('exit', resolve));
        writeFileSync(join(root, 'core-launch.log'), 'private-test-value\nANTHROPIC_API_KEY not set\n');
        return { pid };
      }
    }, 2000), error => String(error).includes('ANTHROPIC_API_KEY not set') && !String(error).includes('private-test-value'));
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('early bind failure reports the configured port without leaking current or old logs', async () => {
  for (const diagnostic of ['CORE_BIND_FAILED port=7437 reason=ACCESS_DENIED', '[winerror10013]', 'CORE_BIND_FAILED port=7437 reason=ADDRESS_IN_USE']) {
    const root = mkdtempSync(join(tmpdir(), 'agentlite-bind-'));
    const port = await freePort();
    writeFileSync(join(root, 'core-launch.log'), 'OLD_API_KEY not set\n');
    try {
      await assert.rejects(ensureCore({ ...options(port), storageDir: root }, {
        preflight: async () => {},
        launch: async () => {
          const child = spawn(process.execPath, ['-e', ''], { windowsHide: true, stdio: 'ignore' });
          const pid = child.pid!; await new Promise(resolve => child.once('exit', resolve));
          writeFileSync(join(root, 'core-launch.log'), `OLD_API_KEY not set\nprivate-test-value\n${diagnostic}\n`);
          return { pid };
        }
      }, 2000), error => {
        const text = String(error);
        return text.includes(`本地端口 ${port}`) && text.includes('agentLite.corePort') &&
          !text.includes('private-test-value') && !text.includes('OLD_API_KEY');
      });
    } finally { rmSync(root, { recursive: true, force: true }); }
  }
});
