/**
 * 本地自检：在框架的 dry-mount 假部署里构造一遍本扩展，检查声明接口。
 * 只用于开发期，不参与打包运行。
 *
 * 跑法（cwd 必须是 Coopanion 的 resources/app，好让 `tsx` 解析得到）：
 *   node --import tsx --import <cortico>/src/extensions/runtime.ts src/selfcheck.ts
 */
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { dryMountWorld } from 'cortico/extensions/dry-mount.ts';
import { checkDescribe } from 'cortico/extensions/describe-export.ts';
import { parseExtensionManifest } from 'cortico/extensions/manifest.ts';
import { readFileSync } from 'node:fs';
import OPEN_XIAOAI from './index.ts';

const scratch = mkdtempSync(join(tmpdir(), 'oxa-dry-'));
const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')) as Record<string, unknown>;

const manifest = parseExtensionManifest(pkg, 'zh');
console.log('manifest:', JSON.stringify(manifest, null, 2));
if (!manifest.ok) {
  console.error('manifest 不合法:', manifest);
  process.exit(1);
}

const report = await dryMountWorld(OPEN_XIAOAI as never, {
  scratchDir: scratch,
  packageDir: new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'),
  language: 'zh',
});console.log('ok:', report.ok);
console.log('warnings:', report.warnings);
console.log('failures:', report.failures);

const pkgDir = new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const describe = await checkDescribe('world', OPEN_XIAOAI as never, 'describe.json', {
  scratchDir: scratch,
  packageDir: pkgDir,
  language: 'zh',
});
console.log('describe ok:', describe.ok);
console.log('describe warnings:', describe.warnings);
console.log('describe failures:', describe.failures);

process.exit(report.failures.length === 0 && describe.ok ? 0 : 1);
