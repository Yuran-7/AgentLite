import * as vscode from 'vscode';
import { createHash } from 'node:crypto';

const scheme = 'agentlite-preview';

export class PreviewDocuments implements vscode.TextDocumentContentProvider {
  private readonly contents = new Map<string, string>();

  // 注册只读虚拟文档，关闭后释放内容，语言切换时仍保留打开的预览。
  constructor(context: Pick<vscode.ExtensionContext, 'subscriptions'>) {
    context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(scheme, this));
    context.subscriptions.push(vscode.workspace.onDidCloseTextDocument(document => {
      if (document.uri.scheme !== scheme) return;
      setImmediate(() => {
        if (!vscode.workspace.textDocuments.some(open => open.uri.toString() === document.uri.toString())) {
          this.contents.delete(document.uri.toString());
        }
      });
    }));
  }

  // 返回预览内容，未登记的地址不能读取磁盘或任意会话。
  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.contents.get(uri.toString()) ?? '';
  }

  // 相同内容复用同一标签，虚拟文档保持未修改状态且无需保存。
  async open(key: string, title: string, content: string, language: string): Promise<void> {
    const digest = createHash('sha256').update(key).update('\0').update(content).digest('hex');
    const uri = vscode.Uri.from({ scheme, path: `/${title}`, query: digest });
    this.contents.set(uri.toString(), content);
    let document = await vscode.workspace.openTextDocument(uri);
    if (document.languageId !== language) document = await vscode.languages.setTextDocumentLanguage(document, language);
    await vscode.window.showTextDocument(document, { preview: true });
  }
}
