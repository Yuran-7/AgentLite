import { test } from 'node:test';
import assert from 'node:assert/strict';
import { BookmarkStore } from '../src/bookmarks';
import type { ChatState } from '../src/session';

// 以最小异步存储替身模拟 VS Code workspaceState 的重载和写入失败。
function storage() {
  let saved: unknown = []; let fail = false;
  return { get: <T>(_key: string, fallback: T): T => (saved ?? fallback) as T,
    update: async (_key: string, value: unknown) => { if (fail) throw new Error('disk full'); saved = value; },
    failing: (value: boolean) => { fail = value; } };
}
// 创建包含相同回答的会话，覆盖内容摘要与出现次序组合的定位规则。
function state(): ChatState {
  return { sessionId: 'session', workspace: 'workspace', connection: 'ready', busy: false,
    sending: false, cancelling: false, model: '', usage: '', cards: [
      { id: 'a', kind: 'assistant', text: 'same response' },
      { id: 'b', kind: 'assistant', text: 'same response' }, { id: 'u', kind: 'user', text: 'question' }] };
}

// 功能：收藏按会话保存，重建和随机卡片标识变化后仍能定位重复回答。
// 设计：使用两份 store 共享存储替身模拟窗口重载，校验出现次序与独立内容快照。
test('bookmarks persist across reload and match restored response occurrences', async () => {
  const saved = storage(); const store = new BookmarkStore(saved); const snapshot = state();
  await store.toggle(snapshot, 'b'); const bookmark = store.list('session')[0];
  assert.equal(bookmark.text, 'same response'); assert.equal(store.matches(snapshot).a, undefined);
  assert.equal(store.matches(snapshot).b, bookmark.id); assert.equal(store.list('other').length, 0);
  snapshot.cards[0].id = 'restored-a'; snapshot.cards[1].id = 'restored-b';
  const restored = new BookmarkStore(saved);
  assert.equal(restored.matches(snapshot)['restored-b'], bookmark.id);
  snapshot.cards = []; assert.equal(restored.list('session')[0].text, 'same response');
  await restored.remove('other', bookmark.id); assert.equal(restored.list('session').length, 1);
  await restored.remove('session', bookmark.id); assert.equal(restored.list('session').length, 0);
});

// 功能：忙碌、非回答及未知标识不可收藏；并发保存不丢失，失败后仍可重试。
// 设计：排队写入两个回答，再注入存储失败验证原记录和后续队列均保持可用。
test('bookmark writes serialize, reject invalid cards and recover from failure', async () => {
  const saved = storage(); const store = new BookmarkStore(saved); const snapshot = state();
  await store.toggle(snapshot, 'u'); await store.toggle(snapshot, 'unknown');
  snapshot.busy = true; await store.toggle(snapshot, 'a'); snapshot.busy = false;
  assert.equal(store.list('session').length, 0);
  await Promise.all([store.toggle(snapshot, 'a'), store.toggle(snapshot, 'b')]);
  assert.equal(store.list('session').length, 2);
  saved.failing(true); await assert.rejects(store.toggle(snapshot, 'a'), /disk full/);
  assert.equal(store.list('session').length, 2);
  saved.failing(false); await store.toggle(snapshot, 'a'); assert.equal(store.list('session').length, 1);
});
