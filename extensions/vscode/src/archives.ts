import type { ChatState } from './session';

type Storage = { get<T>(key: string, fallback: T): T; update(key: string, value: unknown): PromiseLike<void> };
const storageKey = 'agentLite.archivedSessions.v1';

export class ArchiveStore {
  private queue: Promise<void> = Promise.resolve();
  constructor(private readonly storage: Storage) {}

  // 读取工作区归档标记，保留原始会话内容并过滤损坏数据。
  list(): string[] {
    const ids = this.storage.get<unknown>(storageKey, []);
    return Array.isArray(ids) ? [...new Set(ids.filter((id): id is string =>
      typeof id === 'string' && id.length > 0 && id.length < 200))] : [];
  }

  // 串行持久化归档或恢复操作，只接受当前工作区历史中的会话。
  set(state: ChatState, sessionId: string, archived: boolean): Promise<void> {
    if (state.connection !== 'ready' || state.busy || state.sending ||
      !state.history?.some(item => item.session_id === sessionId)) return Promise.resolve();
    const change = this.queue.then(async () => {
      const ids = new Set(this.list());
      if (archived) ids.add(sessionId); else ids.delete(sessionId);
      await this.storage.update(storageKey, [...ids]);
    });
    this.queue = change.catch(() => {});
    return change;
  }
}
