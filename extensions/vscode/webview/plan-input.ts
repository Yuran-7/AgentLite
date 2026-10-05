import type { ChatState } from '../src/session';
import type { PageMessage } from '../src/protocol';

// 每次显示一个问题，回答交给模型后才允许产生下一题。
export function installPlanInput(post: (message: PageMessage) => void): (state: ChatState) => void {
  const dialog = document.createElement('dialog'); dialog.className = 'plan-input-dialog';
  dialog.setAttribute('aria-label', '计划问题'); document.body.append(dialog);
  let active = '';
  dialog.addEventListener('cancel', event => { event.preventDefault(); post({ type: 'cancel' }); });
  return state => {
    const card = state.cards.find(item => item.kind === 'question' && ['pending', 'responding'].includes(item.status ?? ''));
    if (!card) { if (dialog.open) dialog.close(); active = ''; return; }
    if (active !== card.requestId) {
      active = card.requestId!; dialog.replaceChildren();
      const question = card.questions?.[0]; if (!question) return;
      const form = document.createElement('form');
      const heading = document.createElement('div'); heading.className = 'plan-question-heading';
      const title = document.createElement('h3'); title.textContent = question.question;
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'plan-question-close';
      cancel.textContent = '×'; cancel.setAttribute('aria-label', '取消本次任务');
      cancel.addEventListener('click', () => post({ type: 'cancel' }));
      heading.append(title, cancel); form.append(heading);
      const submitAnswer = (answer: string) => {
        if (form.dataset.submitted === 'true') return;
        form.dataset.submitted = 'true';
        form.querySelectorAll<HTMLButtonElement>('button:not(.plan-question-close)').forEach(button => { button.disabled = true; });
        post({ type: 'answerQuestions', requestId: active, answers: { [question.id]: answer } });
      };
      question.options.forEach((option, index) => {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'plan-question-option';
        const number = document.createElement('span'); number.className = 'plan-option-number'; number.textContent = String(index + 1);
        const copy = document.createElement('span'); copy.className = 'plan-option-copy';
        const label = document.createElement('strong'); label.textContent = option.label;
        const description = document.createElement('span'); description.textContent = option.description;
        const arrow = document.createElement('span'); arrow.className = 'plan-option-arrow'; arrow.textContent = '→';
        copy.append(label, description); button.append(number, copy, arrow);
        button.addEventListener('click', () => submitAnswer(option.label)); form.append(button);
      });
      const freeform = document.createElement('div'); freeform.className = 'plan-question-freeform';
      const text = document.createElement('textarea'); text.maxLength = 10000; text.rows = 1;
      text.placeholder = '其他想法，告诉 AgentLite 如何调整'; text.setAttribute('aria-label', '其他方案');
      const submit = document.createElement('button'); submit.type = 'submit'; submit.textContent = '提交';
      const skip = document.createElement('button'); skip.type = 'button'; skip.textContent = 'Skip';
      skip.addEventListener('click', () => submitAnswer('用户跳过此问题。请选择合理默认值并在最终计划中注明假设；仅在必要时继续提问。'));
      freeform.append(text, submit, skip); form.append(freeform);
      const error = document.createElement('div'); error.setAttribute('role', 'alert'); form.append(error);
      form.addEventListener('submit', event => {
        event.preventDefault();
        if (!text.value.trim()) { error.textContent = '请输入你的方案，或选择上方选项。'; text.focus(); return; }
        submitAnswer(text.value.trim());
      });
      dialog.append(form); if (!dialog.open) dialog.showModal();
    }
    const form = dialog.querySelector('form')!;
    if (card.status === 'pending' && state.error) form.dataset.submitted = 'false';
    const waiting = card.status === 'responding' || form.dataset.submitted === 'true';
    form.querySelectorAll<HTMLButtonElement>('button:not(.plan-question-close)').forEach(button => { button.disabled = waiting; });
    dialog.querySelector('[role="alert"]')!.textContent = state.error ?? '';
  };
}
