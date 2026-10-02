// 为整个页面提供统一悬停提示，包括动态按钮、链接和键盘焦点。
export function installTooltips(): void {
  const tooltip = document.createElement('div');
  tooltip.id = 'app-tooltip'; tooltip.className = 'app-tooltip'; tooltip.hidden = true;
  tooltip.setAttribute('role', 'tooltip'); document.body.append(tooltip);
  let active: HTMLElement | undefined;

  // 转换原生 title，覆盖 Markdown 链接等后续插入的元素。
  function migrateTitles(root: ParentNode): void {
    const titled = [...root.querySelectorAll<HTMLElement>('[title]')];
    if (root instanceof HTMLElement && root.hasAttribute('title')) titled.push(root);
    for (const target of titled) {
      target.dataset.tooltip = target.getAttribute('title') || '';
      target.removeAttribute('title');
    }
  }

  // 隐藏气泡并移除临时无障碍描述关联。
  function hide(): void {
    if (active) {
      const ids = (active.getAttribute('aria-describedby') || '').split(/\s+/)
        .filter(id => id && id !== tooltip.id);
      if (ids.length) active.setAttribute('aria-describedby', ids.join(' '));
      else active.removeAttribute('aria-describedby');
    }
    active = undefined; tooltip.hidden = true;
  }

  // 将提示置于按钮附近，并限制在页面边界内。
  function position(): void {
    if (!active?.isConnected || !active.getClientRects().length || active.getAttribute('aria-expanded') === 'true') {
      hide(); return;
    }
    const label = active.dataset.tooltip || '';
    if (!label) { hide(); return; }
    if (tooltip.textContent !== label) tooltip.textContent = label;
    const rect = active.getBoundingClientRect();
    const bubble = tooltip.getBoundingClientRect();
    const left = Math.max(8, Math.min(rect.left + rect.width / 2 - bubble.width / 2,
      window.innerWidth - bubble.width - 8));
    const top = rect.bottom + 6 + bubble.height <= window.innerHeight - 8
      ? rect.bottom + 6 : Math.max(8, rect.top - bubble.height - 6);
    tooltip.style.left = `${left}px`; tooltip.style.top = `${top}px`;
  }

  // 使用事件委托为已有及动态元素显示安全的纯文本提示。
  function show(target: EventTarget | null): void {
    const element = target instanceof Element ? target.closest<HTMLElement>('[data-tooltip], [title]') : null;
    if (!element || element.getAttribute('aria-expanded') === 'true') { hide(); return; }
    migrateTitles(element);
    if (!element.dataset.tooltip) { hide(); return; }
    if (active !== element) hide();
    active = element; tooltip.hidden = false;
    const ids = new Set((element.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
    ids.add(tooltip.id); element.setAttribute('aria-describedby', [...ids].join(' '));
    position();
  }

  migrateTitles(document);
  const observer = new MutationObserver(records => {
    for (const record of records) {
      if (record.type === 'attributes' && record.attributeName === 'title') migrateTitles(record.target as HTMLElement);
      for (const node of record.addedNodes) if (node instanceof HTMLElement) migrateTitles(node);
    }
    if (active) position();
  });
  observer.observe(document.body, { subtree: true, childList: true, attributes: true,
    attributeFilter: ['title', 'data-tooltip', 'aria-expanded'] });
  document.addEventListener('mouseover', event => show(event.target));
  document.addEventListener('mouseout', event => {
    if (active && event.relatedTarget instanceof Node && active.contains(event.relatedTarget)) return;
    hide();
  });
  document.addEventListener('focusin', event => show(event.target));
  document.addEventListener('focusout', hide);
  document.addEventListener('pointerdown', hide, true);
  document.addEventListener('click', hide, true);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') hide(); });
  document.addEventListener('scroll', hide, true);
  window.addEventListener('resize', hide);
  window.addEventListener('blur', hide);
}
