export type ImageAttachment = { name: string; media_type: 'image/png' | 'image/jpeg' | 'image/gif' | 'image/webp'; data: string };
export type CoreEvent = { type: string; [key: string]: unknown };
export const decisions = ['allow_once', 'always_allow', 'deny_once', 'always_deny'] as const;
export type PermissionMode = 'manual' | 'accept_edits' | 'auto';
export type Decision = typeof decisions[number];
export type PageMessage =
  | { type: 'ready' | 'retry' | 'newSession' | 'cancel' | 'openLogs' | 'refreshHistory' | 'openSettings' | 'refreshModels' | 'refreshMcp' | 'refreshSkills' | 'reloadMcp' | 'openMcpSettings' | 'addMcpServer' }
  | { type: 'reconnectMcp'; name: string }
  | { type: 'setMcpEnabled'; name: string; enabled: boolean }
  | { type: 'resumeSession' | 'renameSession' | 'archiveSession' | 'restoreSession'; sessionId: string }
  | { type: 'selectModel'; modelId: string }
  | { type: 'permissionMode'; mode: PermissionMode }
  | { type: 'collaboration'; mode: 'default' | 'plan' }
  | { type: 'implementPlan'; cardId: string }
  | { type: 'answerQuestions'; requestId: string; answers: Record<string, string> }
  | { type: 'reasoning'; effort?: string }
  | { type: 'setMemory'; setting: 'generate' | 'use'; enabled: boolean }
  | { type: 'copyAnswer' | 'openAnswer' | 'copyMessage' | 'downloadPlan'; cardId: string }
  | { type: 'copyCode' | 'openCode'; cardId: string; blockIndex: number }
  | { type: 'toggleBookmark'; cardId: string }
  | { type: 'viewFileChanges'; cardId: string; path?: string }
  | { type: 'undoFileChanges'; cardId: string }
  | { type: 'compactSession' }
  | { type: 'removeBookmark'; bookmarkId: string }
  | { type: 'send'; content: string; images?: ImageAttachment[]; references?: string[] }
  | { type: 'searchWorkspaceFiles'; query: string; requestId: number }
  | { type: 'permission'; toolUseId: string; decision: Decision };

// 只接收页面可执行的有限操作，不允许透传任意 RPC。
export function parsePageMessage(value: unknown): PageMessage | undefined {
  if (!value || typeof value !== 'object') return;
  const message = value as Record<string, unknown>;
  if (message.type === 'permissionMode' && ['manual', 'accept_edits', 'auto'].includes(String(message.mode)))
    return { type: 'permissionMode', mode: message.mode as PermissionMode };
  if (message.type === 'collaboration' && ['default', 'plan'].includes(String(message.mode)))
    return { type: 'collaboration', mode: message.mode as 'default' | 'plan' };
  if (message.type === 'implementPlan' && typeof message.cardId === 'string' && message.cardId.length < 200)
    return { type: 'implementPlan', cardId: message.cardId };
  if (message.type === 'answerQuestions' && typeof message.requestId === 'string' && message.requestId.length < 200 &&
      message.answers && typeof message.answers === 'object' && !Array.isArray(message.answers) &&
      Object.keys(message.answers).length <= 3 && Object.values(message.answers).every(a => typeof a === 'string' && a.trim() && a.length <= 10000))
    return { type: 'answerQuestions', requestId: message.requestId, answers: { ...message.answers as Record<string, string> } };
  if (message.type === 'reasoning') {
    if (message.effort === undefined) return { type: 'reasoning' };
    if (typeof message.effort === 'string' && ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].includes(message.effort)) {
      return { type: 'reasoning', effort: message.effort };
    }
    return;
  }
  if (message.type === 'setMemory' && ['generate', 'use'].includes(String(message.setting)) &&
      typeof message.enabled === 'boolean') {
    return { type: 'setMemory', setting: message.setting as 'generate' | 'use', enabled: message.enabled };
  }
  if (message.type === 'compactSession') return { type: 'compactSession' };
  if (['viewFileChanges', 'undoFileChanges'].includes(String(message.type)) &&
      typeof message.cardId === 'string' && message.cardId.length > 0 && message.cardId.length < 200) {
    if (message.type === 'undoFileChanges') return { type: 'undoFileChanges', cardId: message.cardId };
    if (message.path !== undefined && (typeof message.path !== 'string' || !message.path || message.path.length > 32768)) return;
    return { type: 'viewFileChanges', cardId: message.cardId, path: message.path as string | undefined };
  }
  if (['copyCode', 'openCode'].includes(String(message.type)) && typeof message.cardId === 'string' &&
      message.cardId.length > 0 && message.cardId.length < 200 && Number.isSafeInteger(message.blockIndex) &&
      Number(message.blockIndex) >= 0 && Number(message.blockIndex) < 10_000) {
    return { type: message.type as 'copyCode' | 'openCode', cardId: message.cardId, blockIndex: Number(message.blockIndex) };
  }
  if (['reconnectMcp', 'setMcpEnabled'].includes(String(message.type)) &&
      typeof message.name === 'string' && message.name.length > 0 && message.name.length <= 64) {
    if (message.type === 'reconnectMcp') return { type: 'reconnectMcp', name: message.name };
    if (typeof message.enabled === 'boolean') return { type: 'setMcpEnabled', name: message.name, enabled: message.enabled };
    return;
  }
  if (message.type === 'searchWorkspaceFiles' && typeof message.query === 'string' && message.query.length <= 200 &&
      Number.isSafeInteger(message.requestId) && Number(message.requestId) >= 0) {
    return { type: 'searchWorkspaceFiles', query: message.query, requestId: Number(message.requestId) };
  }
  if (['ready', 'retry', 'newSession', 'cancel', 'openLogs', 'refreshHistory', 'openSettings', 'refreshModels', 'refreshMcp', 'refreshSkills', 'reloadMcp', 'openMcpSettings', 'addMcpServer'].includes(String(message.type))) {
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
  if (['copyAnswer', 'openAnswer', 'copyMessage', 'toggleBookmark', 'downloadPlan'].includes(String(message.type)) &&
      typeof message.cardId === 'string' && message.cardId.length > 0 && message.cardId.length < 200) {
    return { type: message.type as 'copyAnswer' | 'openAnswer' | 'copyMessage' | 'toggleBookmark' | 'downloadPlan', cardId: message.cardId };
  }
  if (message.type === 'send' && typeof message.content === 'string' &&
      (message.content.trim() || (Array.isArray(message.images) && message.images.length > 0)) && message.content.length <= 1_000_000) {
    if (message.references !== undefined && (!Array.isArray(message.references) || message.references.length > 20 ||
        !message.references.every(path => typeof path === 'string' && path.length > 0 && path.length <= 1024))) return;
    if (message.images !== undefined) {
      if (!Array.isArray(message.images) || message.images.length > 4 || !message.images.every(image =>
        image && typeof image.name === 'string' && image.name.length <= 255 &&
        ['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(image.media_type) &&
        typeof image.data === 'string' && image.data.length > 0 && image.data.length <= 4 * 1024 * 1024 &&
        image.data.length % 4 === 0 && /^[A-Za-z0-9+/]*={0,2}$/.test(image.data))) return;
      return { type: 'send', content: message.content.trim(), images: message.images,
        ...(message.references ? { references: message.references } : {}) };
    }
    return { type: 'send', content: message.content.trim(),
      ...(message.references ? { references: message.references } : {}) };
  }
  if (message.type === 'permission' && typeof message.toolUseId === 'string' &&
      decisions.includes(message.decision as Decision)) {
    return { type: 'permission', toolUseId: message.toolUseId, decision: message.decision as Decision };
  }
}
