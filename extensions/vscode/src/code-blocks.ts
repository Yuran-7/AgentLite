import { Marked } from 'marked';

// 按 Markdown 渲染顺序提取围栏及缩进代码，宿主操作始终取自原始回答。
export function codeBlocks(text: string): { text: string; language: string }[] {
  const parser = new Marked();
  const result: { text: string; language: string }[] = [];
  parser.walkTokens(parser.lexer(text), token => {
    if (token.type === 'code') result.push({ text: token.text, language: token.lang?.split(/\s/)[0] ?? '' });
  });
  return result;
}
