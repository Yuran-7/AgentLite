export type CoreEvent = { type: string; [key: string]: unknown };
export const decisions = ['allow_once', 'always_allow', 'deny_once', 'always_deny'] as const;
export type Decision = typeof decisions[number];
export type PageMessage =
  | { type: 'ready' | 'retry' | 'newSession' | 'cancel' | 'openLogs' }
  | { type: 'send'; content: string }
  | { type: 'permission'; toolUseId: string; decision: Decision };

// 只接收页面可执行的有限操作，不允许透传任意 RPC。
export function parsePageMessage(value: unknown): PageMessage | undefined {
  if (!value || typeof value !== 'object') return;
  const message = value as Record<string, unknown>;
  if (['ready', 'retry', 'newSession', 'cancel', 'openLogs'].includes(String(message.type))) {
    return { type: message.type as 'ready' };
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
