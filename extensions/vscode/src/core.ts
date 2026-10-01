import { spawn, execFile } from 'node:child_process';
import { existsSync, mkdirSync, openSync, closeSync, readFileSync, writeFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { homedir } from 'node:os';
import { lock } from 'proper-lockfile';
import { RpcClient } from './rpc';

export type CoreOptions = { workspace: string; pythonPath: string; port: number; storageDir: string; coreDirectory?: string };
export type CoreConnection = { client: RpcClient; origin: 'reused' | 'started' };
export type RuntimeInfo = { pythonPath: string; coreDirectory: string };
export type Runtime = { preflight: (python: string, cwd: string) => Promise<RuntimeInfo | void>;
  launch: (python: string, options: CoreOptions) => Promise<{ pid: number } | void> };
const execute = promisify(execFile);
export const runtime: Runtime = {
  // 检查解释器与模块可用性，不加载模型配置、不输出环境变量。
  async preflight(python, cwd) {
    try {
      const script = [
        'import sys, json; from pathlib import Path',
        'assert sys.version_info[:2] == (3, 12), "Python 3.12 required"',
        'import agent_lite, importlib.util',
        'assert importlib.util.find_spec("agent_lite.core.app") is not None, "AgentLite core required"',
        'package = Path(agent_lite.__file__).resolve().parent',
        'project = next((p for p in (package.parent, package.parent.parent) if (p / "pyproject.toml").is_file()), None)',
        'print(json.dumps({"pythonPath": sys.executable, "coreDirectory": str(project or (Path.home() / ".agentlite"))}))'
      ].join('\n');
      const result = await execute(python, ['-c', script],
        { cwd, windowsHide: true, timeout: 15_000, maxBuffer: 64 * 1024 });
      return JSON.parse(result.stdout.trim()) as RuntimeInfo;
    } catch {
      throw new Error('需要已安装 AgentLite 的 Python 3.12。请设置 agentLite.pythonPath；在 AgentLite 仓库运行 uv sync，或在目标环境运行 python -m pip install -e <AgentLite仓库路径>。');
    }
  },
  // 启动独立后台进程，将标准流交给文件，退出 VS Code 后 core 仍可运行。
  async launch(python, options) {
    mkdirSync(options.storageDir, { recursive: true });
    const cwd = options.coreDirectory || options.workspace;
    if (process.platform === 'win32') {
      // Windows 的 Node detached 会创建控制台；短命启动器用原生无窗口标志创建独立 core。
      const bootstrap = [
        'import subprocess, sys',
        'with open(sys.argv[2], "ab", buffering=0) as log:',
        '    child = subprocess.Popen([sys.executable, "-m", "agent_lite.core"], cwd=sys.argv[1], stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)',
        '    log.write(("AgentLite background pid=%d\\n" % child.pid).encode())',
        'print(child.pid)'
      ].join('\n');
      const result = await execute(python, ['-c', bootstrap, cwd, join(options.storageDir, 'core-launch.log')], {
        cwd, windowsHide: true, timeout: 15_000, maxBuffer: 64 * 1024,
        env: { ...process.env, AGENTLITE_HOST: '127.0.0.1', AGENTLITE_PORT: String(options.port), AGENTLITE_FRONTEND_MANAGED: '1' }
      });
      return { pid: Number(result.stdout.trim()) };
    }
    const fd = openSync(join(options.storageDir, 'core-launch.log'), 'a');
    try {
      return await new Promise<{ pid: number }>((resolve, reject) => {
        const child = spawn(python, ['-m', 'agent_lite.core'], {
          cwd, shell: false, windowsHide: true, detached: true,
          stdio: ['ignore', fd, fd],
          env: { ...process.env, AGENTLITE_HOST: '127.0.0.1', AGENTLITE_PORT: String(options.port), AGENTLITE_FRONTEND_MANAGED: '1' }
        });
        child.once('error', reject);
        child.once('spawn', () => { child.unref(); resolve({ pid: child.pid! }); });
      });
    } finally { closeSync(fd); }
  }
};

// 按显式设置、工作区虚拟环境、PATH 的顺序选择 Python。
export function selectPython(workspace: string, configured: string, platform = process.platform): string {
  if (configured.trim()) return configured.trim();
  const candidate = join(workspace, '.venv', platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  return existsSync(candidate) ? candidate : 'python';
}

// 确认端口属于可用 core，连接成功但握手失败时不得启动第二个进程。
async function probe(port: number, timeoutMs: number): Promise<RpcClient | undefined> {
  let client: RpcClient;
  try { client = await RpcClient.connect(port, timeoutMs); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ECONNREFUSED') return;
    throw error;
  }
  try {
    const pong = await client.request('core.ping', { client: 'vscode' }, timeoutMs);
    if (typeof pong.server_version !== 'string' || !pong.server_version) throw new Error('无效 core.ping 响应');
    return client;
  } catch {
    client.close();
    throw new Error(`端口 ${port} 已有服务，但无法通过 core.ping；请检查 core 版本或更换 agentLite.corePort。`);
  }
}

// 优先复用共享 core，必要时启动并等待监听；并发启动输掉端口竞争也可连接赢家。
export async function ensureCore(options: CoreOptions, dependencies = runtime, startupMs = 30_000): Promise<CoreConnection> {
  const locks = join(homedir(), '.agentlite', 'locks');
  mkdirSync(locks, { recursive: true });
  const target = join(locks, `core-127.0.0.1-${options.port}.start`);
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const existing = await probe(options.port, 1500);
    if (existing) return { client: existing, origin: 'reused' };
    let release: (() => Promise<void>) | undefined;
    let compromised: Error | undefined;
    try {
      release = await lock(target, { realpath: false, stale: 60_000, update: 10_000,
        onCompromised: error => { compromised = error; } });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ELOCKED') throw error;
      await new Promise(resolve => setTimeout(resolve, 150));
      continue;
    }
    try {
      return await ensureCoreLocked(options, {
        preflight: dependencies.preflight,
        launch: async (python, args) => {
          if (compromised) throw new Error('core 启动锁失效，请重试。');
          return dependencies.launch(python, args);
        }
      }, startupMs);
    } finally { await release().catch(() => {}); }
  }
  throw new Error('等待其他窗口启动 core 超时，请查看日志后重试。');
}

// 持有跨窗口启动锁后再次探测，只有唯一启动者执行 Python 预检和启动。
async function ensureCoreLocked(options: CoreOptions, dependencies: Runtime, startupMs: number): Promise<CoreConnection> {
  const existing = await probe(options.port, 1500);
  if (existing) return { client: existing, origin: 'reused' };
  mkdirSync(options.storageDir, { recursive: true });
  const cacheFile = join(options.storageDir, 'runtime.json');
  let cachedPython = '';
  try { cachedPython = String(JSON.parse(readFileSync(cacheFile, 'utf8')).pythonPath || ''); } catch { /* 首次运行或缓存失效时重新发现。 */ }
  const candidates = options.pythonPath.trim() ? [options.pythonPath.trim()] :
    [...new Set([cachedPython, selectPython(options.workspace, ''), 'python'].filter(Boolean))];
  let python = ''; let info: RuntimeInfo | void = undefined; let lastError: unknown;
  for (const candidate of candidates) {
    try { info = await dependencies.preflight(candidate, options.storageDir); python = info?.pythonPath || candidate; break; }
    catch (error) { lastError = error; }
  }
  if (!python) throw lastError;
  const coreDirectory = options.coreDirectory?.trim() || info?.coreDirectory || options.workspace;
  if (options.coreDirectory?.trim() && (!existsSync(coreDirectory) || !statSync(coreDirectory).isDirectory())) {
    throw new Error('agentLite.coreDirectory 必须是已存在的 core 配置目录。');
  }
  mkdirSync(coreDirectory, { recursive: true });
  if (info) writeFileSync(cacheFile, JSON.stringify({ pythonPath: python }));
  const logfile = join(options.storageDir, 'core-launch.log');
  const logOffset = existsSync(logfile) ? statSync(logfile).size : 0;
  const launched = await dependencies.launch(python, { ...options, coreDirectory });
  const deadline = Date.now() + startupMs;
  while (Date.now() < deadline) {
    const client = await probe(options.port, Math.min(1000, Math.max(1, deadline - Date.now())));
    if (client) return { client, origin: 'started' };
    if (launched?.pid) {
      let alive = true;
      try { process.kill(launched.pid, 0); } catch (error) { alive = (error as NodeJS.ErrnoException).code !== 'ESRCH'; }
      if (!alive) {
        let detail = '';
        try { detail = readFileSync(logfile).subarray(logOffset).toString('utf8').match(/\b[A-Z][A-Z0-9_]*_API_KEY not set\b/)?.[0] || ''; } catch { /* 日志入口仍可用于诊断。 */ }
        throw new Error(`core 启动后退出。配置目录：${coreDirectory}。${detail || '请查看启动日志。'}`);
      }
    }
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error('core 未在 30 秒内就绪。请查看启动日志及 ~/.agentlite/logs/core.log，检查模型配置后重试。');
}
