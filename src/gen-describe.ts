/**
 * 生成 describe.json：用框架自己的 exportDescribe 产出，保证与代码逐字节一致。
 *
 * 跑法（cwd 必须是 Coopanion 的 resources/app）：
 *   node --import tsx --import <cortico>/src/extensions/runtime.ts --input-type=module -e "await import('file:///.../gen-describe.ts')"
 */
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { exportDescribe } from 'cortico/extensions/describe-export.ts';
import OPEN_XIAOAI from './index.ts';

const scratch = mkdtempSync(join(tmpdir(), 'oxa-desc-'));
const pkgDir = new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');

const file = await exportDescribe('world', OPEN_XIAOAI as never, {
  scratchDir: scratch,
  packageDir: pkgDir,
  language: 'zh',
});
if (!file) {
  console.error('定义里没有 describe，生成不了。');
  process.exit(1);
}
writeFileSync(join(pkgDir, 'describe.json'), JSON.stringify(file, null, 2) + '\n', 'utf8');
console.log('describe.json 已重新生成，工具数:', (file.tools ?? []).length);
