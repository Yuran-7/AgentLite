import hljs from 'highlight.js/lib/core';
import python from 'highlight.js/lib/languages/python';
import powershell from 'highlight.js/lib/languages/powershell';
import javascript from 'highlight.js/lib/languages/javascript';
import typescript from 'highlight.js/lib/languages/typescript';
import json from 'highlight.js/lib/languages/json';
import bash from 'highlight.js/lib/languages/bash';
import xml from 'highlight.js/lib/languages/xml';
import css from 'highlight.js/lib/languages/css';
import sql from 'highlight.js/lib/languages/sql';
import yaml from 'highlight.js/lib/languages/yaml';
import diff from 'highlight.js/lib/languages/diff';

for (const [name, grammar] of Object.entries({ python, powershell, javascript, typescript, json, bash, xml, css, sql, yaml, diff })) hljs.registerLanguage(name, grammar);
const labels: Record<string, string> = { python: 'Python', py: 'Python', javascript: 'JavaScript', js: 'JavaScript', typescript: 'TypeScript', ts: 'TypeScript', json: 'JSON', html: 'HTML', xml: 'XML', css: 'CSS', sql: 'SQL', yaml: 'YAML', yml: 'YAML', powershell: 'PowerShell', ps1: 'PowerShell', bash: 'Bash', sh: 'Shell', shell: 'Shell', text: 'Text', plaintext: 'Text' };
const aliases: Record<string, string> = { shell: 'bash', ps1: 'powershell' };

// 给安全 Markdown 中的代码块增加语言标题、本地语法高亮与可选的宿主操作。
export function decorateCodeBlocks(container: HTMLElement, action?: (index: number, type: 'copyCode' | 'openCode') => void): void {
  container.querySelectorAll<HTMLElement>('pre > code').forEach((code, index) => {
    const pre = code.parentElement!;
    const language = [...code.classList].find(name => name.startsWith('language-'))?.slice(9).toLowerCase() ?? '';
    const grammar = aliases[language] ?? language;
    const text = code.textContent ?? '';
    if (grammar && hljs.getLanguage(grammar) && text.length <= 100_000) code.innerHTML = hljs.highlight(text, { language: grammar, ignoreIllegals: true }).value;
    const block = document.createElement('section'); block.className = 'code-block';
    const header = document.createElement('div'); header.className = 'code-heading';
    const label = document.createElement('span'); label.className = 'code-language';
    const symbol = document.createElement('span'); symbol.className = 'code-symbol'; symbol.textContent = '‹/›'; symbol.setAttribute('aria-hidden', 'true');
    label.append(symbol, document.createTextNode(labels[language] ?? (language || 'Text')));
    header.append(label);
    if (action) {
      const actions = document.createElement('div'); actions.className = 'code-actions';
      for (const [type, title, path] of [
        ['openCode', '在编辑器中打开代码', '<path d="M14 3h7v7m0-7-8 8M10 21H3v-7m0 7 8-8"/>'],
        ['copyCode', '复制代码', '<rect x="4" y="8" width="12" height="13" rx="2"/><path d="M8 8V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-2"/>']
      ] as const) {
        const button = document.createElement('button'); button.className = 'icon-button';
        button.dataset.tooltip = title; button.setAttribute('aria-label', title);
        button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
        button.addEventListener('click', () => action(index, type)); actions.append(button);
      }
      header.append(actions);
    }
    pre.replaceWith(block); block.append(header, pre);
  });
}
