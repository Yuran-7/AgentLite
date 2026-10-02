import { createHash, randomUUID } from 'node:crypto';
import type { ChatState } from './session';

export type Bookmark = { id: string; sessionId: string; sourceKey: string; text: string; savedAt: string; writtenAt?: string };
type Storage = { get<T>(key: string, fallback: T): T; update(key: string, value: unknown): PromiseLike<void> };
const storageKey = 'agentLite.responseBookmarks.v1';

// 校验本地记录的必要字段，不让损坏数据阻断收藏操作。
function valid(item: unknown): item is Bookmark {
  if (!item || typeof item !== 'object') return false;
  const value = item as Bookmark;
  return typeof value.id === 'string' && typeof value.sessionId === 'string' && typeof value.sourceKey === 'string' &&
    typeof value.text === 'string' && typeof value.savedAt === 'string';
}

// 用回答内容与同内容出现次序定位原文，避免恢复历史时随机卡片标识失效。
function sourceKey(state: ChatState, cardId: string): string | undefined {
  const card = state.cards.find(item => item.id === cardId && item.kind === 'assistant');
  if (!card?.text.trim()) return;
  const identical = state.cards.filter(item => item.kind === 'assistant' && item.text === card.text);
  return `${createHash('sha256').update(card.text).digest('hex')}:${identical.findIndex(item => item.id === cardId)}`;
}

export class BookmarkStore {
  private queue: Promise<void> = Promise.resolve();
  constructor(private readonly storage: Storage) {}

  // 读取当前会话的收藏，过滤损坏记录而不将其他会话混入面板。
  list(sessionId?: string): Bookmark[] {
    const saved = this.storage.get<unknown>(storageKey, []);
    if (!sessionId || !Array.isArray(saved)) return [];
    return saved.filter(valid).filter(item => item.sessionId === sessionId);
  }

  // 将收藏映射到当前展示的回答，找不到原文时仍保留独立的内容快照。
  matches(state: ChatState): Record<string, string> {
    const result: Record<string, string> = {};
    const saved = new Map(this.list(state.sessionId).map(item => [item.sourceKey, item.id]));
    for (const card of state.cards) {
      if (card.kind !== 'assistant') continue;
      const id = saved.get(sourceKey(state, card.id) ?? ''); if (id) result[card.id] = id;
    }
    return result;
  }

  // 串行切换已完成回答的收藏，持久化成功后才更新页面状态。
  toggle(state: ChatState, cardId: string): Promise<void> {
    const card = state.cards.find(item => item.id === cardId && item.kind === 'assistant');
    const key = sourceKey(state, cardId); const sessionId = state.sessionId;
    if (!card || !key || !sessionId || state.busy || state.sending) return Promise.resolve();
    const entry: Bookmark = { id: randomUUID(), sessionId, sourceKey: key, text: card.text,
      savedAt: new Date().toISOString(), writtenAt: card.createdAt };
    return this.mutate(saved => {
      const existing = saved.find(item => item.sessionId === sessionId && item.sourceKey === key);
      return existing ? saved.filter(item => item.id !== existing.id) : [...saved, entry];
    });
  }

  // 只移除当前会话内指定的收藏，其他会话记录保持完整。
  remove(sessionId: string | undefined, bookmarkId: string): Promise<void> {
    return this.mutate(saved => saved.filter(item => item.sessionId !== sessionId || item.id !== bookmarkId));
  }

  // 排队执行本地存储写入，失败不覆盖现有记录且不阻塞下一次操作。
  private mutate(change: (saved: Bookmark[]) => Bookmark[]): Promise<void> {
    const operation = this.queue.then(async () => {
      const saved = this.storage.get<Bookmark[]>(storageKey, []);
      await this.storage.update(storageKey, change(Array.isArray(saved) ? saved.filter(valid) : []));
    });
    this.queue = operation.catch(() => {}); return operation;
  }
}
