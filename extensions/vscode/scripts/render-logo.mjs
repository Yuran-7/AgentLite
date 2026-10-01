import { readFileSync, writeFileSync } from 'node:fs';
import { Resvg } from '@resvg/resvg-js';

// 从矢量源生成插件列表使用的高清 PNG，保持两个格式一致。
const source = new URL('../media/logo.svg', import.meta.url);
const target = new URL('../media/icon.png', import.meta.url);
const renderer = new Resvg(readFileSync(source), { fitTo: { mode: 'width', value: 256 } });
writeFileSync(target, renderer.render().asPng());
