export type FileChange = { id: string; path: string; run_id: string; operation: string; status: string; restores?: string };
export type ChangedFile = { path: string; changeIds: string[]; added: number; removed: number };
export type FileSummary = { files: ChangedFile[]; undone: boolean; busy?: boolean; error?: string };

// 只统计补丁正文的增删行，排除文件头和上下文。
export function diffStats(diff: string): { added: number; removed: number } {
  let added = 0; let removed = 0; let hunk = false;
  for (const line of diff.split('\n')) {
    if (line.startsWith('@@')) { hunk = true; continue; }
    if (!hunk) continue;
    if (line.startsWith('+')) added++;
    else if (line.startsWith('-')) removed++;
  }
  return { added, removed };
}

// 将同一运行的历史按文件合并，保留提交顺序用于逆序撤销。
export function summarizeChanges(changes: FileChange[], diffs: Map<string, string>): Map<string, FileSummary> {
  const restored = new Set(changes.filter(item => item.status === 'committed' && item.restores).map(item => item.restores));
  const runs = new Map<string, FileSummary>();
  for (const change of changes) {
    if (change.status !== 'committed' || change.operation === 'restore' || !change.run_id) continue;
    let summary = runs.get(change.run_id);
    if (!summary) { summary = { files: [], undone: true }; runs.set(change.run_id, summary); }
    let file = summary.files.find(item => item.path === change.path);
    if (!file) { file = { path: change.path, changeIds: [], added: 0, removed: 0 }; summary.files.push(file); }
    const stats = diffStats(diffs.get(change.id) ?? '');
    file.added = stats.added; file.removed = stats.removed;
    if (!restored.has(change.id)) { file.changeIds.push(change.id); summary.undone = false; }
  }
  return runs;
}
