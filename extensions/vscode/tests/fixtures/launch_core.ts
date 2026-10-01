import { ensureCore } from '../../src/core';

// 用短生命周期宿主启动真实共享 core，退出后由外部测试检查 core 仍可连接。
async function main(): Promise<void> {
  const [workspace, pythonPath, port, coreDirectory = workspace, storageDir = workspace] = process.argv.slice(2);
  const connection = await ensureCore({ workspace, coreDirectory, pythonPath, port: Number(port), storageDir });
  connection.client.close();
  process.stdout.write(connection.origin);
}
void main().catch(error => { console.error(error.message); process.exitCode = 1; });
