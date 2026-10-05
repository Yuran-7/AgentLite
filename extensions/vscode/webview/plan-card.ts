import type { Card, ChatState } from '../src/session';
import type { PageMessage } from '../src/protocol';

const expanded = new Set<string>();

// 把最终方案展示为独立卡片，展开状态在宿主刷新期间保持。
export function renderPlanCard(card: Card, plan: string, markdown: (text: string) => string,
  post: (message: PageMessage) => void, state?: ChatState): HTMLElement {
  const panel = document.createElement('section'); panel.className = 'proposed-plan';
  panel.setAttribute('aria-label', 'Plan');
  const header = document.createElement('div'); header.className = 'proposed-plan-header';
  const label = document.createElement('strong'); label.textContent = 'Plan'; header.append(label);
  const actions = document.createElement('div'); actions.className = 'proposed-plan-actions';
  for (const [type, title, text] of [
    ['downloadPlan', '下载计划', '↓'], ['copyAnswer', '复制计划', '⧉'], ['openAnswer', '打开计划', 'Open ↗']
  ] as const) {
    const button = document.createElement('button'); button.type = 'button';
    button.textContent = text; button.setAttribute('aria-label', title); button.dataset.tooltip = title;
    button.addEventListener('click', () => post({ type, cardId: card.id })); actions.append(button);
  }
  const collapse = document.createElement('button'); collapse.type = 'button';
  collapse.setAttribute('aria-label', '展开或收起计划'); actions.append(collapse);
  header.append(actions); panel.append(header);
  const body = document.createElement('div'); body.className = 'markdown proposed-plan-body';
  body.id = `plan-body-${card.id}`; body.innerHTML = markdown(plan); panel.append(body);
  const expand = document.createElement('button'); expand.type = 'button';
  expand.className = 'proposed-plan-expand'; expand.textContent = 'Expand plan'; panel.append(expand);
  const update = () => {
    const open = expanded.has(card.id); panel.classList.toggle('expanded', open);
    collapse.textContent = open ? '⌃' : '⌄'; collapse.setAttribute('aria-expanded', String(open));
    collapse.setAttribute('aria-controls', body.id); expand.hidden = open;
  };
  const toggle = () => { if (expanded.has(card.id)) expanded.delete(card.id); else expanded.add(card.id); update(); };
  collapse.addEventListener('click', toggle); expand.addEventListener('click', toggle); update();
  if (card.completed && state?.collaborationMode === 'plan') {
    const execute = document.createElement('button'); execute.type = 'button';
    execute.className = 'proposed-plan-execute'; execute.textContent = '开始执行计划';
    execute.disabled = !!state.busy || !!state.sending;
    execute.addEventListener('click', () => post({ type: 'implementPlan', cardId: card.id })); panel.append(execute);
  }
  return panel;
}
