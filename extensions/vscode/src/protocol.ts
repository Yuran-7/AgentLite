export type CoreEvent = { type: string; [key: string]: unknown };
export const decisions = ['allow_once', 'always_allow', 'deny_once', 'always_deny'] as const;
export type Decision = typeof decisions[number];
export type PageMessage =
  | { type: 'ready' | 'retry' | 'newSession' | 'cancel' | 'openLogs' | 'refreshHistory' | 'openSettings' | 'refreshModels' }
  | { type: 'resumeSession' | 'renameSession' | 'archiveSession' | 'restoreSession'; sessionId: string }
  | { type: 'selectModel'; modelId: string }
  | { type: 'copyAnswer' | 'openAnswer' | 'copyMessage'; cardId: string }
  | { type: 'copyCode' | 'openCode'; cardId: string; blockIndex: number }
  | { type: 'toggleBookmark'; cardId: string }
  | { type: 'removeBookmark'; bookmarkId: string }
  | { type: 'send'; content: string }
  | { type: 'permission'; toolUseId: string; decision: Decision };

// 只接收页面可执行的有限操作，不允许透传任意 RPC。
export function parsePageMessage(value: unknown): PageMessage | undefined {
  if (!value || typeof value !== 'object') return;
  const message = value as Record<string, unknown>;
  if (['copyCode', 'openCode'].includes(String(message.type)) && typeof message.cardId === 'string' &&
      message.cardId.length > 0 && message.cardId.length < 200 && Number.isSafeInteger(message.blockIndex) &&
      Number(message.blockIndex) >= 0 && Number(message.blockIndex) < 10_000) {
    return { type: message.type as 'copyCode' | 'openCode', cardId: message.cardId, blockIndex: Number(message.blockIndex) };
  }
  if (['ready', 'retry', 'newSession', 'cancel', 'openLogs', 'refreshHistory', 'openSettings', 'refreshModels'].includes(String(message.type))) {
    return { type: message.type as 'ready' };
  }
  if (['resumeSession', 'renameSession', 'archiveSession', 'restoreSession'].includes(String(message.type)) &&
      typeof message.sessionId === 'string' && message.sessionId.length > 0 && message.sessionId.length < 200) {
    return { type: message.type as 'resumeSession' | 'renameSession' | 'archiveSession' | 'restoreSession', sessionId: message.sessionId };
  }
  if (message.type === 'selectModel' && typeof message.modelId === 'string' && message.modelId.length < 200) {
    return { type: 'selectModel', modelId: message.modelId };
  }
  if (message.type === 'removeBookmark' && typeof message.bookmarkId === 'string' &&
      message.bookmarkId.length > 0 && message.bookmarkId.length < 200) {
    return { type: 'removeBookmark', bookmarkId: message.bookmarkId };
  }
  if (['copyAnswer', 'openAnswer', 'copyMessage', 'toggleBookmark'].includes(String(message.type)) &&
      typeof message.cardId === 'string' && message.cardId.length > 0 && message.cardId.length < 200) {
    return { type: message.type as 'copyAnswer' | 'openAnswer' | 'copyMessage' | 'toggleBookmark', cardId: message.cardId };
  }
  if (message.type === 'send' && typeof message.content === 'string' &&
      message.content.trim() && message.content.length <= 1_000_000) {
    return { type: 'send', content: message.content.trim() };
  }
  if (message.type === 'permission' && typeof message.toolUseId === 'string' &&
      decisions.includes(message.decision as Decision)) {
    return { type: 'permission', toolUseId: message.toolUseId, decision: message.decision as Decision };
  }
}
