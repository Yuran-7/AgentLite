import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { gitWorkspacePaths, searchWorkspaceEntries, workspaceEntries } from '../src/workspace-files';

// 功能：文件索引遵守根目录和子目录的 .gitignore，包含未忽略的新增文件。
// 设计：创建真实临时 Git 工作区，验证忽略规则而非模拟过滤结果。
test('workspace index respects nested gitignore rules', async t => {
  const root = mkdtempSync(join(tmpdir(), 'agentlite-files-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  execFileSync('git', ['init', '-q'], { cwd: root });
  mkdirSync(join(root, 'src', 'generated'), { recursive: true });
  mkdirSync(join(root, 'ignored'), { recursive: true });
  writeFileSync(join(root, '.gitignore'), 'ignored/\n*.log\n');
  writeFileSync(join(root, 'src', '.gitignore'), 'generated/\n');
  writeFileSync(join(root, 'src', 'main.ts'), 'export {}');
  writeFileSync(join(root, 'src', 'generated', 'out.ts'), 'ignored');
  writeFileSync(join(root, 'ignored', 'secret.ts'), 'ignored');
  writeFileSync(join(root, 'debug.log'), 'ignored');
  const paths = await gitWorkspacePaths(root);
  assert(paths.includes('src/main.ts'));
  assert(!paths.some(path => path.includes('generated') || path.includes('ignored') || path.endsWith('.log')));
});

// 功能：空搜索只列顶层，名称搜索返回匹配项和父目录，目录路径只列直属项目。
// 设计：用同一小树检查结果边界、图标类型所需的目录类型和隐藏文件。
test('workspace search avoids flattening directories', () => {
  const entries = workspaceEntries(['.gitignore', 'README.md', 'extensions/vscode/src/extension.ts',
    'extensions/vscode/src/session.ts', 'extensions/vscode/tests/session.test.ts', 'src/main.ts']);
  assert.deepEqual(searchWorkspaceEntries(entries, '').map(entry => entry.path), ['extensions/', 'src/', 'README.md']);
  assert.deepEqual(searchWorkspaceEntries(entries, 'extens').map(entry => entry.path), [
    'extensions/', 'extensions/vscode/', 'extensions/vscode/src/', 'extensions/vscode/src/extension.ts'
  ]);
  assert.deepEqual(searchWorkspaceEntries(entries, 'extensions/vscode/').map(entry => entry.path), [
    'extensions/vscode/src/', 'extensions/vscode/tests/'
  ]);
  assert.deepEqual(searchWorkspaceEntries(entries, '.gitignore').map(entry => entry.path), ['.gitignore']);
});
