// 生成只引用打包资源的受限 Webview 文档，便于独立验证页面行为。
export function webviewHtml(nonce: string, source: string, script: string, style: string): string {
  return `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${source}; script-src 'nonce-${nonce}';">
    <link rel="stylesheet" href="${style}"><title>AgentLite</title></head>
    <body><header><div class="brand">AgentLite <span class="badge">DEMO</span></div>
    <div id="workspace"></div><div id="connection" role="status">等待连接</div>
    <nav><button id="new" disabled>新建会话</button><button id="retry">重试</button><button id="logs">日志</button></nav></header>
    <div id="error" role="alert" hidden></div><main id="cards" aria-label="聊天记录"></main>
    <footer><div id="stats"></div><textarea id="input" rows="3" disabled placeholder="给 AgentLite 一个任务…" aria-label="消息"></textarea>
    <div class="composer"><span>Enter 发送 · Shift+Enter 换行</span><button id="stop" disabled>停止</button><button id="send" disabled>发送</button></div></footer>
    <script nonce="${nonce}" src="${script}"></script></body></html>`;
}
