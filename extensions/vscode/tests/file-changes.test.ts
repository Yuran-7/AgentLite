import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { diffStats, summarizeChanges, type FileChange } from '../src/file-changes';
import { ChatSession } from '../src/session';
import { parsePageMessage } from '../src/protocol';

const patch = '--- a\n+++ b\n@@ -1 +1,2 @@\n-old\n+new\n+++content\n';
const records: FileChange[] = [
  { id: 'first', path: 'C:/workspace/a.ts', run_id: 'run', operation: 'write', status: 'committed' },
  { id: 'second', path: 'C:/workspace/a.ts', run_id: 'run', operation: 'edit', status: 'committed' },
  { id: 'third', path: 'C:/workspace/b.ts', run_id: 'run', operation: 'write', status: 'committed' },
];

// 功能：统计净补丁并合并同一文件，恢复记录和未提交修改不作为新文件。
// 设计：用以加号开头的真实内容验证文件头过滤，重复编辑只采用最新范围差异。
test('file summaries use net ranges and retain persistent undo markers', () => {
  assert.deepEqual(diffStats(patch), { added: 2, removed: 1 });
  const summary = summarizeChanges([...records,
    { ...records[0], id: 'restore', operation: 'restore', restores: 'second' },
    { ...records[0], id: 'pending', status: 'pending' },
  ], new Map([['first', patch], ['second', patch], ['third', patch]])).get('run')!;
  assert.equal(summary.files.length, 2);
  assert.equal(summary.files[0].added, 2);
  assert.deepEqual(summary.files[0].changeIds, ['first']);
  assert(!summary.undone);
});

// 功能：撤销按全局提交顺序倒序执行，部分冲突不重试已恢复记录且不允许伪造卡片。
// 设计：真实会话状态配合 RPC 替身，模拟一个文件撤销成功后另一个文件发生外部修改。
test('host diff and undo validate cards, preserve partial conflicts and survive refresh', async () => {
  class Client extends EventEmitter {
    restored: string[] = []; listed = [...records];
    async request(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
      if (method === 'file_history.list') return { changes: this.listed };
      if (method === 'file_history.diff') return { diff: patch };
      if (method === 'file_history.restore') {
        const id = String(params.change_id);
        if (id === 'second') throw new Error('Restore conflict: external modifications');
        this.restored.push(id);
        this.listed.push({ ...records[0], id: 'restore-third', operation: 'restore', restores: id });
      }
      return {};
    }
    close(): void {}
  }
  const client = new Client(); const session = new ChatSession('C:/workspace', () => {}, client);
  Object.assign(session.state, { sessionId: 'session', connection: 'ready' });
  await session.refreshFileChanges();
  const card = session.state.cards[0];
  assert.equal(session.fileChangesDiff('forged'), undefined);
  assert.equal(session.fileChangesDiff(card.id, 'outside.ts'), '');
  assert.equal(session.fileChangesDiff(card.id, records[0].path), patch);
  await session.undoFileChanges('forged'); assert.deepEqual(client.restored, []);
  session.state.busy = true; await session.undoFileChanges(card.id); assert.deepEqual(client.restored, []);
  session.state.busy = false; await session.undoFileChanges(card.id);
  assert.deepEqual(client.restored, ['third']);
  assert(card.fileSummary?.error?.includes('external modifications'));
  assert.deepEqual(card.fileSummary?.files[1].changeIds, []);
  assert(!card.fileSummary?.busy);
  await session.undoFileChanges(card.id); assert.deepEqual(client.restored, ['third']);
  assert.equal(parsePageMessage({ type: 'viewFileChanges', cardId: card.id, path: 123 }), undefined);
  await session.dispose();
});
