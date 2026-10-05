// 收敛 DeepSeek 的实际档位，兼容旧 core 返回的通用档位列表。
export function modelEfforts(model: string, efforts: string[]): string[] {
  if (claudeEfforts(model).length) return claudeEfforts(model);
  return /deepseek-(?:flash|v4)/i.test(model) ? ['none', 'low', 'high', 'max'] : efforts;
}

// 使用模型允许的默认强度，DeepSeek 的 Medium 等价于 High。
export function modelEffort(model: string, effort = ''): string {
  if (/^(?:glm-5\.3(?:-flash)?|kimi-k3)$/i.test(model)) {
    if (effort === 'none' || effort === 'minimal') return 'low';
    if (effort === 'medium') return 'high';
    if (!effort || effort === 'xhigh') return 'max';
  }
  if (/deepseek-(?:flash|v4)/i.test(model)) {
    if (effort === 'minimal') return 'low';
    if (!effort || effort === 'medium' || effort === 'xhigh') return 'high';
  }
  return effort || 'medium';
}
// 识别支持 Effort 的 Anthropic 协议型号，旧型号保持原有请求行为。
export function claudeEfforts(model: string): string[] {
  if (/^(?:glm-5\.3(?:-flash)?|kimi-k3)$/i.test(model)) return ['low', 'high', 'max'];
  if (/^deepseek-v4\.1-flash$/i.test(model)) return ['none', 'low', 'high', 'max'];
  if (/claude-(?:opus-(?:5|4-[78])|sonnet-5|fable-5|mythos-5)/i.test(model))
    return ['low', 'medium', 'high', 'xhigh', 'max'];
  if (/claude-(?:opus|sonnet)-4-6/i.test(model)) return ['low', 'medium', 'high', 'max'];
  if (/claude-opus-4-5/i.test(model)) return ['low', 'medium', 'high'];
  return [];
}

