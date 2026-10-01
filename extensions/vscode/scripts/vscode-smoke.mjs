import { build } from 'esbuild';
import { mkdtempSync, mkdirSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn } from 'node:child_process';

const executable = process.env.VSCODE_EXECUTABLE;
if (!executable || !existsSync(executable)) throw new Error('请设置 VSCODE_EXECUTABLE 为本地 VS Code 可执行文件的绝对路径。');
await build({ entryPoints: ['tests/vscode-smoke.ts'], outfile: 'dist/vscode-smoke.js', bundle: true,
  platform: 'node', format: 'cjs', target: 'node20', external: ['vscode'] });
const directory = mkdtempSync(join(tmpdir(), 'agentlite-vscode-smoke-'));
const workspace = join(directory, 'workspace'); mkdirSync(workspace);
const environment = { ...process.env }; delete environment.ELECTRON_RUN_AS_NODE;
let child;
try {
  child = spawn(executable, ['--user-data-dir', join(directory, 'profile'), '--extensions-dir', join(directory, 'extensions'),
    '--disable-extensions', '--disable-workspace-trust', '--skip-welcome', '--skip-release-notes',
    `--extensionDevelopmentPath=${resolve('.')}`, `--extensionTestsPath=${resolve('dist/vscode-smoke.js')}`, workspace],
    { windowsHide: true, env: environment, stdio: ['ignore', 'pipe', 'pipe'] });
  child.stdout.on('data', chunk => process.stdout.write(chunk));
  child.stderr.on('data', chunk => process.stderr.write(chunk));
  const deadline = setTimeout(() => { child.kill(); }, 60_000);
  const exitCode = await new Promise((resolveExit, reject) => {
    child.once('error', reject); child.once('exit', code => resolveExit(code));
  });
  clearTimeout(deadline);
  if (exitCode !== 0) throw new Error(`VS Code smoke test exit: ${exitCode}`);
} finally {
  // 仅清理本脚本创建的独立 profile，避免碰到用户真实 VS Code 配置。
  for (let attempt = 0; attempt < 20; attempt++) {
    try { rmSync(directory, { recursive: true, force: true }); break; }
    catch { await new Promise(resolveWait => setTimeout(resolveWait, 100)); }
  }
}
