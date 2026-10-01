import DOMPurify from 'dompurify';
import { Marked } from 'marked';
const parser = new Marked({ breaks: true });
parser.use({ renderer: {
  html({ text }) { return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
} });

// 禁用原始 HTML，并限制 Markdown 输出为安全的文本格式标签。
export function markdown(text: string): string {
  return DOMPurify.sanitize(parser.parse(text, { async: false }), {
    ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'blockquote', 'ul', 'ol', 'li',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a'],
    ALLOWED_ATTR: ['href', 'title'], ALLOW_DATA_ATTR: false
  });
}
