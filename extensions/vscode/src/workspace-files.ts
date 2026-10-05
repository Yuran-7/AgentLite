import { execFile } from 'node:child_process';

export type WorkspaceEntry = { path: string; kind: 'directory' | 'file' };

const binary = /\.(?:png|jpe?g|gif|webp|bmp|ico|mp[34]|mov|wav|zip|tar|gz|exe|dll|pyc)$/i;

// 执行以 NUL 分隔路径的索引命令，保留文件名中的空格和换行。
async function indexedPaths(command: string, args: string[], root: string): Promise<string[]> {
  const output = await new Promise<Buffer>((resolve, reject) => {
    execFile(command, args,
      { cwd: root, encoding: 'buffer', maxBuffer: 16 * 1024 * 1024 }, (error, stdout) => {
        if (error) reject(error);
        else resolve(Buffer.isBuffer(stdout) ? stdout : Buffer.from(stdout));
      });
  });
  return output.toString('utf8').split('\0').filter(Boolean);
}

// 读取 Git 跟踪及未忽略的新增文件，遵守各层 .gitignore 和全局排除规则。
export async function gitWorkspacePaths(root: string): Promise<string[]> {
  return indexedPaths('git', ['ls-files', '--cached', '--others', '--exclude-standard', '-z'], root);
}

// 非 Git 工作区使用 ripgrep 的 ignore 规则列出文件。
export async function ripgrepWorkspacePaths(root: string): Promise<string[]> {
  return indexedPaths('rg', ['--files', '--hidden', '-0', '--glob', '!.git/**'], root);
}

// 从可引用文件生成各层目录，避免将整个目录树平铺给页面。
export function workspaceEntries(paths: string[]): WorkspaceEntry[] {
  const files = new Set<string>();
  const directories = new Set<string>();
  for (const raw of paths) {
    const path = raw.replaceAll('\\', '/').replace(/^\.\//, '');
    if (!path || path.startsWith('/') || path.split('/').includes('..') || binary.test(path)) continue;
    files.add(path);
    const parts = path.split('/');
    for (let end = 1; end < parts.length; end++) directories.add(`${parts.slice(0, end).join('/')}/`);
  }
  return [
    ...[...directories].map(path => ({ path, kind: 'directory' as const })),
    ...[...files].map(path => ({ path, kind: 'file' as const })),
  ];
}

// 空搜索只显示顶层；输入名称时显示匹配项及其父目录；目录后加 / 只看直属项目。
export function searchWorkspaceEntries(entries: WorkspaceEntry[], rawQuery: string, limit = 40): WorkspaceEntry[] {
  const query = rawQuery.trim().toLowerCase().replaceAll('\\', '/').replace(/^@/, '');
  const byPath = new Map(entries.map(entry => [entry.path, entry]));
  const depth = (path: string) => path.split('/').filter(Boolean).length;
  const hidden = (entry: WorkspaceEntry) => entry.path.replace(/\/$/, '').split('/').at(-1)!.startsWith('.');
  const order = (a: WorkspaceEntry, b: WorkspaceEntry) =>
    (a.kind === b.kind ? 0 : a.kind === 'directory' ? -1 : 1) || depth(a.path) - depth(b.path) || a.path.localeCompare(b.path);
  if (!query) return entries.filter(entry => depth(entry.path) === 1 && !hidden(entry)).sort(order).slice(0, limit);
  if (query.endsWith('/') && byPath.has(query)) {
    return entries.filter(entry => entry.path.startsWith(query) && depth(entry.path) === depth(query) + 1 && !hidden(entry))
      .sort(order).slice(0, limit);
  }
  const nameQuery = query.split('/').at(-1) || query;
  const matched = entries.filter(entry => {
    const name = entry.path.replace(/\/$/, '').split('/').at(-1)!.toLowerCase();
    return query.includes('/') ? entry.path.toLowerCase().includes(query) && name.includes(nameQuery)
      : name.includes(nameQuery);
  }).sort(order).slice(0, limit);
  const result = new Map<string, WorkspaceEntry>();
  for (const entry of matched) {
    const parts = entry.path.replace(/\/$/, '').split('/');
    for (let end = 1; end < parts.length; end++) {
      const parent = byPath.get(`${parts.slice(0, end).join('/')}/`);
      if (parent) result.set(parent.path, parent);
    }
    result.set(entry.path, entry);
  }
  return [...result.values()].sort(order).slice(0, limit);
}
