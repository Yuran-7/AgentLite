import type { Card } from './session';

// 从工具参数提取命令或文件正文，页面预览与编辑器使用同一份内容。
export function toolPresentation(card: Card): { name: string; subject: string; input: string; output?: string; lines?: number; layout: 'read' | 'write' | 'io' } {
  const params: Record<string, unknown> = card.params && typeof card.params === 'object'
    ? card.params as Record<string, unknown> : {};
  const title = card.title ?? 'Tool';
  if (card.kind === 'subagent' || title === 'spawn_agent') return {
    name: 'Agent', subject: String(params.description ?? card.text), layout: 'io',
    input: String(params.prompt ?? card.text),
  };
  if (title === 'web_search' || title === 'web_fetch') {
    let output = card.output;
    if (output !== undefined && card.status !== 'failed') {
      try {
        const payload: unknown = JSON.parse(output);
        if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
          const data = payload as Record<string, unknown>;
          if (title === 'web_search' && Array.isArray(data.results)) {
            const results = data.results.filter((item): item is Record<string, unknown> =>
              !!item && typeof item === 'object' && !Array.isArray(item));
            output = results.length ? results.map((item, index) => [
              typeof item.title === 'string' ? `${index + 1}. ${item.title}` : '',
              typeof item.url === 'string' ? item.url : '',
              typeof item.snippet === 'string' ? item.snippet : '',
            ].filter(Boolean).join('\n')).join('\n\n') : 'No results found.';
          } else if (title === 'web_fetch' && typeof data.content === 'string') {
            output = [typeof data.title === 'string' ? data.title : '', data.content].filter(Boolean).join('\n\n');
          }
        }
      } catch { /* 非 JSON 错误与旧版本输出保留原文。 */ }
    }
    return { name: title, subject: '', layout: 'io',
      input: typeof params[title === 'web_search' ? 'query' : 'url'] === 'string'
        ? params[title === 'web_search' ? 'query' : 'url'] as string : JSON.stringify(params, null, 2), output };
  }
  const shell = /^(shell|bash|exec_command)$/i.test(title);
  const write = /^(write|write_file)$/i.test(title);
  const read = /^(read|read_file)$/i.test(title);
  const command = params.command ?? params.cmd;
  const path = params.path ?? params.file_path;
  const content = params.content;
  return {
    name: shell ? 'Bash' : write ? 'Write' : read ? 'Read' : title,
    layout: read ? 'read' : write ? 'write' : 'io',
    subject: typeof path === 'string' ? path : typeof params.description === 'string' ? params.description : '',
    input: shell && typeof command === 'string' ? command : write && typeof content === 'string'
      ? content : JSON.stringify(params, null, 2),
    lines: write && typeof content === 'string' ? content.split('\n').length : undefined,
  };
}
