// 生成只引用打包资源的受限 Webview 文档，便于独立验证页面行为。
export function webviewHtml(nonce: string, source: string, script: string, style: string, logo = 'logo.svg'): string {
  return `<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${source} data:; style-src ${source}; script-src 'nonce-${nonce}';">
    <link rel="stylesheet" href="${style}"><title>AgentLite</title></head>
    <body><header><div class="topline">
    <button id="session-title" disabled aria-label="重命名当前会话">新会话</button>
    <div class="session-actions">
    <button id="bookmark" class="icon-button" data-tooltip="收藏的回答" aria-label="收藏的回答" aria-expanded="false" aria-controls="bookmarks-panel"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 21V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v16l-6-4z"/></svg></button>
    <button id="history-toggle" class="icon-button" aria-expanded="false" aria-controls="history-panel" data-tooltip="历史会话" aria-label="历史会话"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></svg></button>
    <button id="new" class="icon-button" disabled data-tooltip="新建会话" aria-label="新建会话"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 11.5a9 9 0 0 1-9 9H3l2.4-4.2A9 9 0 1 1 21 11.5Z M12 7v8m-4-4h8"/></svg></button>
    </div></div></header>
    <section id="history-panel" hidden aria-label="历史会话"><div class="history-heading"><strong id="history-heading">历史聊天</strong><div class="history-heading-actions"><button id="history-archives" aria-pressed="false" aria-label="查看已归档会话">已归档</button><button id="history-refresh" data-tooltip="刷新历史">刷新</button></div></div>
    <input id="history-search" type="search" placeholder="搜索会话名称…" aria-label="搜索历史会话"><div id="history-list"></div></section>
    <div class="chat-layout"><div class="chat-main"><div id="error" role="alert" hidden></div><main id="cards" data-logo="${logo}" aria-label="聊天记录"><div id="run-status" role="status" hidden><span id="run-label"></span></div><div id="idle-thinking" role="status" hidden><span class="progress-shimmer">Processing</span></div></main>
    <footer>
    <div class="composer-shell">
    <section id="mcp-panel" class="popover mcp-panel" hidden role="dialog" aria-labelledby="mcp-title">
    <div class="mcp-heading"><strong id="mcp-title">MCP 服务器</strong><div class="mcp-heading-actions"><button id="mcp-refresh">刷新</button><button id="mcp-close" aria-label="关闭 MCP 面板">×</button></div></div>
    <div id="mcp-summary" role="status"></div><div id="mcp-message" role="status" hidden></div><div id="mcp-servers"></div>
    <div class="mcp-footer"><button id="mcp-add">添加服务器</button><button id="mcp-configure">编辑配置</button><button id="mcp-apply">应用配置</button></div></section>
    <div id="slash-menu" class="popover slash-menu" hidden><div id="slash-options" role="listbox" aria-label="快捷命令"></div></div>
    <div id="file-mention-menu" class="popover file-mention-menu" hidden><div class="menu-heading">引用工作区文件</div><input id="file-mention-search" type="search" placeholder="搜索文件名或路径…" aria-label="搜索工作区文件" aria-controls="file-mention-options" hidden><div id="file-mention-options" role="listbox" aria-label="工作区文件"></div><div id="file-mention-empty" hidden></div><div class="file-mention-help">选中后发送，AgentLite 会先读取文件或查看目录。</div></div>
    <section id="command-detail" class="popover command-detail" hidden role="dialog" aria-labelledby="command-title"><div class="command-heading"><strong id="command-title"></strong><button id="command-close" aria-label="关闭命令详情">×</button></div><div id="command-body"></div></section>
    <div id="image-attachments" class="image-attachments" hidden></div><div id="image-error" role="alert" hidden></div><div id="file-reference-error" role="alert" hidden></div><div id="file-reference-chips" aria-label="已引用的工作区文件" hidden></div><input id="image-picker" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden>
    <textarea id="input" rows="1" disabled placeholder="向 AgentLite 提问，输入 / 查看命令，@ 引用文件…" aria-label="消息" aria-haspopup="listbox" aria-expanded="false" aria-controls="slash-options file-mention-options"></textarea>
    <div class="composer-toolbar">
    <div class="attachment-control"><button id="attach" class="icon-button" aria-label="添加上下文" data-tooltip="添加上下文" aria-expanded="false" aria-controls="attachment-menu"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v18M3 12h18"/></svg></button>
    <div id="attachment-menu" class="popover" hidden><div class="menu-heading">添加上下文</div><button id="upload-images"><span>上传图片</span><span class="menu-note">也可粘贴或拖入</span></button><button id="reference-workspace-file"><span>引用工作区文件</span><span class="menu-note">也可输入 @ 搜索</span></button></div></div>
    <div class="permission-control"><button id="permission-toggle" class="icon-button" data-tooltip="权限模式" aria-label="权限模式" aria-haspopup="menu" aria-expanded="false" aria-controls="permission-menu" disabled>
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 3 7v5c0 5 4 8 9 10 5-2 9-5 9-10V7z"/><path d="m8 12 3 3 5-6"/></svg></button>
    <div id="permission-menu" class="popover" hidden role="menu" aria-label="权限模式"></div></div>
    <div class="connection-control"><button id="connection-toggle" class="icon-button" data-tooltip="连接信息" aria-label="连接信息" aria-expanded="false" aria-controls="connection-menu"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/></svg></button>
    <div id="connection-menu" class="popover" hidden role="region" aria-label="连接详情"><div class="menu-heading">连接信息</div><div class="connection-content"><div id="connection" role="status">等待连接</div><div id="connection-counts" role="status" hidden></div><div class="connection-label">当前工作区</div><div id="workspace"></div></div><nav class="connection-actions"><button id="retry">重新连接</button><button id="logs">查看日志</button></nav></div></div>
    <span class="toolbar-space"></span><div class="model-control"><select id="model-select" aria-label="选择模型" hidden disabled><option value="" disabled>选择模型</option></select>
    <button id="model-toggle" class="text-control" disabled aria-haspopup="dialog" aria-expanded="false" aria-controls="model-menu"><span id="model-label">选择模型</span><span id="model-effort"></span><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg></button>
    <div id="model-menu" class="popover" hidden role="dialog" aria-label="模型与推理强度"><div class="menu-heading">Select a model</div><div id="model-options" role="listbox" aria-label="可用模型"></div>
    <div id="effort-control"><div class="effort-row"><label id="effort-label" for="effort-slider">Effort</label><div class="effort-track"><div id="effort-dots" aria-hidden="true"></div><input id="effort-slider" type="range" min="0" max="4" step="1" value="1" aria-label="推理强度" disabled></div></div>
    <div class="effort-footer"><span id="effort-message" role="status"></span></div></div></div></div>
    <button id="stop" class="round-action" hidden disabled data-tooltip="停止生成" aria-label="停止"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1"/></svg></button>
    <button id="send" class="round-action" disabled data-tooltip="发送消息" aria-label="发送"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-7 7 7-7 7 7"/></svg></button>
    </div></div><div id="stats"></div></footer></div>
    <aside id="bookmarks-panel" hidden aria-label="收藏的回答"><div class="bookmarks-heading"><strong>收藏的回答</strong><button id="bookmarks-close" class="icon-button" data-tooltip="关闭收藏" aria-label="关闭收藏"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></div><div id="bookmarks-body"></div></aside></div>
    <script nonce="${nonce}" src="${script}"></script></body></html>`;
}
