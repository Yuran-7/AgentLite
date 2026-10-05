import type { ChatState } from '../src/session';
import type { PageMessage } from '../src/protocol';
import { claudeEfforts, modelEffort, modelEfforts } from '../src/reasoning';

export const effortLabels: Record<string, string> = {
  low: 'Light', medium: 'Medium', high: 'High', xhigh: 'Extra High', max: 'Ultra',
  none: 'None', minimal: 'Minimal', '': '',
};

export function installEffortControl(post: (message: PageMessage) => void) {
  const menu = document.getElementById('model-menu')!;
  const toggle = document.getElementById('model-toggle')!;
  const control = document.getElementById('effort-control')!;
  const label = document.getElementById('effort-label')!;
  const slider = document.getElementById('effort-slider') as HTMLInputElement;
  const dots = document.getElementById('effort-dots')!;
  const message = document.getElementById('effort-message')!;
  let latest: ChatState | undefined;
  let levels: string[] = [];
  let lastReadIdentity = '';
  let preview: string | undefined;
  let restoreSliderFocus = false;
  function request(): void {
    if (!latest || latest.connection !== 'ready' || latest.sending || latest.busy) return;
    lastReadIdentity = JSON.stringify([latest.sessionId, latest.selectedModel]);
    post({ type: 'reasoning' });
  }
  slider.addEventListener('input', () => {
    if (slider.disabled) return;
    preview = levels[Number(slider.value)];
    label.textContent = `Effort (${effortLabels[preview || '']})`;
    slider.setAttribute('aria-valuetext', effortLabels[preview || '']);
  });
  slider.addEventListener('change', () => {
    if (slider.disabled) return;
    const effort = levels[Number(slider.value)];
    if (effort === undefined) return;
    preview = effort; restoreSliderFocus = true;
    post({ type: 'reasoning', effort });
  });
  function render(state: ChatState): void {
    const identity = JSON.stringify([state.sessionId, state.selectedModel]);
    if (latest?.sessionId !== state.sessionId || latest?.selectedModel !== state.selectedModel) preview = undefined;
    latest = state;
    const data = state.reasoningOptions;
    levels = modelEfforts(data?.model ?? '', data?.efforts ?? []);
    const profile = state.models?.find(model => model.id === state.selectedModel) ?? state.fallbackModel;
    const openai = profile?.protocol === 'openai' || data?.supported ||
      (profile?.protocol === 'anthropic' && claudeEfforts(profile.model).length > 0);
    control.hidden = !openai && data !== undefined && !state.reasoningError;
    const locked = state.connection !== 'ready' || state.busy || state.sending || state.mcpBusy;
    slider.disabled = !!locked || !data?.supported || levels.length < 2 || !!state.reasoningError;
    const effective = modelEffort(data?.model || profile?.model || '', state.reasoningEffort || data?.effectiveEffort || profile?.reasoningEffort);
    if (state.reasoningError || (!state.reasoningLoading && document.activeElement !== slider)) preview = undefined;
    const shown = preview ?? effective;
    label.textContent = shown ? `Effort (${effortLabels[shown] || shown})` : 'Effort';
    slider.setAttribute('aria-valuetext', effortLabels[shown] || shown);
    slider.max = String(Math.max(0, levels.length - 1));
    const index = levels.indexOf(shown);
    if (preview === undefined) slider.value = String(index >= 0 ? index : Math.max(0, levels.indexOf('medium')));
    const signature = JSON.stringify(levels);
    if (dots.dataset.signature !== signature) {
      dots.dataset.signature = signature; dots.replaceChildren();
      for (const _ of levels) dots.append(document.createElement('span'));
    }
    message.textContent = state.reasoningError || (state.reasoningLoading ? '更新中…'
      : !data ? '读取推理设置…' : !data.supported ? '当前模型不支持推理设置'
      : state.busy || state.sending ? '任务运行中' : '');
    message.hidden = !message.textContent;
    if (!menu.hidden && !locked && identity !== lastReadIdentity) request();
    if (restoreSliderFocus && !menu.hidden && !locked) { slider.focus(); restoreSliderFocus = false; }
  }
  function open(focusEffort = true): void {
    menu.hidden = false; toggle.setAttribute('aria-expanded', 'true');
    document.getElementById('mcp-panel')!.hidden = true;
    preview = undefined;
    if (focusEffort) { restoreSliderFocus = true; if (!slider.disabled) slider.focus(); }
    request();
  }
  return { open, render };
}
