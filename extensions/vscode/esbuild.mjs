import { build } from 'esbuild';
await Promise.all([
  build({ entryPoints: ['src/extension.ts'], outfile: 'dist/extension.js', bundle: true,
    platform: 'node', format: 'cjs', target: 'node20', external: ['vscode'], sourcemap: true }),
  build({ entryPoints: ['webview/main.ts'], outfile: 'dist/webview.js', bundle: true,
    platform: 'browser', target: 'es2022', sourcemap: true })
]);
