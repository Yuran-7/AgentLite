import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ArchiveStore } from '../src/archives';
import type { ChatState } from '../src/session';

// 功能：归档持久化后可恢复，并发归档不丢记录，未知或忙碌会话不能修改。
// 设计：使用异步工作区存储替身并重新创建读取器，覆盖重载、写入失败与串行更新。
test('archives persist, restore, serialize writes and validate session IDs', async () => {
  let stored: unknown = []; let fail = false;
  const storage = {
    get<T>(_key: string, fallback: T): T { return (stored ?? fallback) as T; },
    async update(_key: string, value: unknown): Promise<void> {
      await new Promise(resolve => setTimeout(resolve, 0));
      if (fail) throw new Error('disk failed');
      stored = value;
    }
  };
  const state: ChatState = { workspace: 'workspace', connection: 'ready', busy: false,
    sending: false, cancelling: false, model: '', usage: '', cards: [], history: [
      { session_id: 'a', title: 'A', updated_at: '' }, { session_id: 'b', title: 'B', updated_at: '' }
    ] };
  const archive = new ArchiveStore(storage);
  await Promise.all([archive.set(state, 'a', true), archive.set(state, 'b', true)]);
  assert.deepEqual(new ArchiveStore(storage).list(), ['a', 'b']);
  await archive.set(state, 'a', false); assert.deepEqual(archive.list(), ['b']);
  await archive.set(state, 'unknown', true); assert.deepEqual(archive.list(), ['b']);
  state.busy = true; await archive.set(state, 'a', true); assert.deepEqual(archive.list(), ['b']);
  state.busy = false; fail = true;
  await assert.rejects(archive.set(state, 'a', true), /disk failed/);
  assert.deepEqual(archive.list(), ['b']);
  fail = false; await archive.set(state, 'a', true); assert.deepEqual(archive.list(), ['b', 'a']);
  stored = ['a', 'a', null, 123, '']; assert.deepEqual(archive.list(), ['a']);
});
